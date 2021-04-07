# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import collections
from functools import partial
import itertools
import operator
from unittest import SkipTest

from absl.testing import absltest
from absl.testing import parameterized

import numpy as np

import jax
from jax import api
from jax import core
from jax import dtypes
from jax import lax
from jax import test_util as jtu
from jax import tree_util
from jax._src import lax_reference
from jax.test_util import check_grads
import jax.util
from jax._src.util import prod
from jax import xla

from jax._src.lax.lax import _device_put_raw


from jax.config import config
config.parse_flags_with_absl()


### lax tests

# For standard unops and binops, we can generate a large number of tests on
# arguments of appropriate shapes and dtypes using the following table.

float_dtypes = jtu.dtypes.all_floating
complex_elem_dtypes = jtu.dtypes.floating
complex_dtypes = jtu.dtypes.complex
inexact_dtypes = jtu.dtypes.all_inexact
int_dtypes = jtu.dtypes.all_integer
uint_dtypes = jtu.dtypes.all_unsigned
bool_dtypes = jtu.dtypes.boolean
default_dtypes = float_dtypes + int_dtypes
all_dtypes = float_dtypes + complex_dtypes + int_dtypes + uint_dtypes + bool_dtypes
python_scalar_types = [bool, int, float, complex]

compatible_shapes = [[(3,)], [(3, 4), (3, 1), (1, 4)], [(2, 3, 4), (2, 1, 4)]]


OpRecord = collections.namedtuple(
    "OpRecord", ["op", "nargs", "dtypes", "rng_factory", "tol"])

def op_record(op, nargs, dtypes, rng_factory, tol=None):
  return OpRecord(op, nargs, dtypes, rng_factory, tol)

LAX_OPS = [
    op_record("neg", 1, default_dtypes + complex_dtypes, jtu.rand_small),
    op_record("sign", 1, default_dtypes + uint_dtypes, jtu.rand_small),
    op_record("floor", 1, float_dtypes, jtu.rand_small),
    op_record("ceil", 1, float_dtypes, jtu.rand_small),
    op_record("round", 1, float_dtypes, jtu.rand_default),
    op_record("nextafter", 2, [f for f in float_dtypes if f != dtypes.bfloat16],
              jtu.rand_default, tol=0),

    op_record("is_finite", 1, float_dtypes, jtu.rand_small),

    op_record("exp", 1, float_dtypes + complex_dtypes, jtu.rand_small),
    # TODO(b/142975473): on CPU, expm1 for float64 is only accurate to ~float32
    # precision.
    op_record("expm1", 1, float_dtypes + complex_dtypes, jtu.rand_small,
              {np.float64: 1e-8}),
    op_record("log", 1, float_dtypes + complex_dtypes, jtu.rand_positive),
    op_record("log1p", 1, float_dtypes + complex_dtypes, jtu.rand_positive),
    # TODO(b/142975473): on CPU, tanh for complex128 is only accurate to
    # ~float32 precision.
    # TODO(b/143135720): on GPU, tanh has only ~float32 precision.
    op_record("tanh", 1, float_dtypes + complex_dtypes, jtu.rand_small,
              {np.float64: 1e-9, np.complex128: 1e-7}),
    op_record("sin", 1, float_dtypes + complex_dtypes, jtu.rand_default),
    op_record("cos", 1, float_dtypes + complex_dtypes, jtu.rand_default),
    op_record("atan2", 2, float_dtypes, jtu.rand_default),

    op_record("sqrt", 1, float_dtypes + complex_dtypes, jtu.rand_positive),
    op_record("rsqrt", 1, float_dtypes + complex_dtypes, jtu.rand_positive),
    op_record("square", 1, float_dtypes + complex_dtypes, jtu.rand_default),
    op_record("reciprocal", 1, float_dtypes + complex_dtypes, jtu.rand_positive),
    op_record("tan", 1, float_dtypes + complex_dtypes, jtu.rand_default, {np.float32: 3e-5}),
    op_record("asin", 1, float_dtypes + complex_dtypes, jtu.rand_small),
    op_record("acos", 1, float_dtypes + complex_dtypes, jtu.rand_small),
    op_record("atan", 1, float_dtypes + complex_dtypes, jtu.rand_small),
    op_record("asinh", 1, float_dtypes + complex_dtypes, jtu.rand_default,
              tol={np.complex64: 1E-4, np.complex128: 1E-5}),
    op_record("acosh", 1, float_dtypes + complex_dtypes, jtu.rand_positive),
    # TODO(b/155331781): atanh has only ~float precision
    op_record("atanh", 1, float_dtypes + complex_dtypes, jtu.rand_small, {np.float64: 1e-9}),
    op_record("sinh", 1, float_dtypes + complex_dtypes, jtu.rand_default),
    op_record("cosh", 1, float_dtypes + complex_dtypes, jtu.rand_default),
    op_record("lgamma", 1, float_dtypes, jtu.rand_positive,
              {np.float32: 1e-3 if jtu.device_under_test() == "tpu" else 1e-5,
               np.float64: 1e-14}),
    op_record("digamma", 1, float_dtypes, jtu.rand_positive,
              {np.float64: 1e-14}),
    op_record("betainc", 3, float_dtypes, jtu.rand_positive,
              {np.float64: 1e-14}),
    op_record("igamma", 2,
              [f for f in float_dtypes if f not in [dtypes.bfloat16, np.float16]],
              jtu.rand_positive, {np.float64: 1e-14}),
    op_record("igammac", 2,
              [f for f in float_dtypes if f not in [dtypes.bfloat16, np.float16]],
              jtu.rand_positive, {np.float64: 1e-14}),
    op_record("erf", 1, float_dtypes, jtu.rand_small),
    op_record("erfc", 1, float_dtypes, jtu.rand_small),
    # TODO(b/142976030): the approximation of erfinf used by XLA is only
    # accurate to float32 precision.
    op_record("erf_inv", 1, float_dtypes, jtu.rand_small,
              {np.float64: 1e-9}),
    op_record("bessel_i0e", 1, float_dtypes, jtu.rand_default),
    op_record("bessel_i1e", 1, float_dtypes, jtu.rand_default),

    op_record("real", 1, complex_dtypes, jtu.rand_default),
    op_record("imag", 1, complex_dtypes, jtu.rand_default),
    op_record("complex", 2, complex_elem_dtypes, jtu.rand_default),
    op_record("conj", 1, complex_elem_dtypes + complex_dtypes,
              jtu.rand_default),
    op_record("abs", 1, default_dtypes + complex_dtypes, jtu.rand_default),
    op_record("pow", 2, float_dtypes + complex_dtypes, jtu.rand_positive),

    op_record("bitwise_and", 2, bool_dtypes, jtu.rand_small),
    op_record("bitwise_not", 1, bool_dtypes, jtu.rand_small),
    op_record("bitwise_or", 2, bool_dtypes, jtu.rand_small),
    op_record("bitwise_xor", 2, bool_dtypes, jtu.rand_small),
    op_record("population_count", 1, int_dtypes + uint_dtypes, jtu.rand_int),
    op_record("clz", 1, int_dtypes + uint_dtypes, jtu.rand_int),

    op_record("add", 2, default_dtypes + complex_dtypes, jtu.rand_small),
    op_record("sub", 2, default_dtypes + complex_dtypes, jtu.rand_small),
    op_record("mul", 2, default_dtypes + complex_dtypes, jtu.rand_small),
    op_record("div", 2, default_dtypes + complex_dtypes, jtu.rand_nonzero),
    op_record("rem", 2, default_dtypes, jtu.rand_nonzero),

    op_record("max", 2, all_dtypes, jtu.rand_small),
    op_record("min", 2, all_dtypes, jtu.rand_small),

    op_record("eq", 2, all_dtypes, jtu.rand_some_equal),
    op_record("ne", 2, all_dtypes, jtu.rand_small),
    op_record("ge", 2, default_dtypes, jtu.rand_small),
    op_record("gt", 2, default_dtypes, jtu.rand_small),
    op_record("le", 2, default_dtypes, jtu.rand_small),
    op_record("lt", 2, default_dtypes, jtu.rand_small),
]


class LaxTest(jtu.JaxTestCase):
  """Numerical tests for LAX operations."""

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
        {"testcase_name": jtu.format_test_name_suffix(
            rec.op, shapes, itertools.repeat(dtype)),
         "op_name": rec.op, "rng_factory": rec.rng_factory, "shapes": shapes,
         "dtype": dtype}
        for shape_group in compatible_shapes
        for shapes in itertools.combinations_with_replacement(shape_group, rec.nargs)
        for dtype in rec.dtypes)
      for rec in LAX_OPS))
  def testOp(self, op_name, rng_factory, shapes, dtype):
    rng = rng_factory(self.rng())
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    op = getattr(lax, op_name)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
        {"testcase_name": jtu.format_test_name_suffix(
            rec.op, shapes, itertools.repeat(dtype)),
         "op_name": rec.op, "rng_factory": rec.rng_factory, "shapes": shapes,
         "dtype": dtype, "tol": rec.tol}
        for shape_group in compatible_shapes
        for shapes in itertools.combinations_with_replacement(shape_group, rec.nargs)
        for dtype in rec.dtypes)
      for rec in LAX_OPS))
  def testOpAgainstNumpy(self, op_name, rng_factory, shapes, dtype, tol):
    if (not config.x64_enabled and op_name == "nextafter"
        and dtype == np.float64):
      raise SkipTest("64-bit mode disabled")
    rng = rng_factory(self.rng())
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    op = getattr(lax, op_name)
    numpy_op = getattr(lax_reference, op_name)
    self._CheckAgainstNumpy(numpy_op, op, args_maker, tol=tol)

  # TODO test shift_left, shift_right_arithmetic, shift_right_logical

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}_weak_type={}".format(
          from_dtype, to_dtype, weak_type),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "weak_type": weak_type}
      for from_dtype, to_dtype in itertools.product(
          [None, np.float32, np.int32, "float32", "int32"], repeat=2)
      for weak_type in [True, False]))
  def testConvertElementType(self, from_dtype, to_dtype, weak_type):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax._convert_element_type(x, to_dtype, weak_type)
    self._CompileAndCheck(op, args_maker)

    x = rng((1,), from_dtype)
    out = op(x)
    self.assertEqual(out.dtype, dtypes.canonicalize_dtype(to_dtype or x.dtype))
    self.assertEqual(out.aval.weak_type, weak_type)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}"
       .format(from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype}
      for from_dtype, to_dtype in itertools.product(
          [np.float32, np.int32, "float32", "int32"], repeat=2)))
  def testConvertElementTypeAgainstNumpy(self, from_dtype, to_dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.convert_element_type(x, to_dtype)
    numpy_op = lambda x: lax_reference.convert_element_type(x, to_dtype)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}"
       .format(from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype}
      for from_dtype, to_dtype in itertools.product(
          [np.float32, np.int32, "float32", "int32"], repeat=2)))
  def testBitcastConvertType(self, from_dtype, to_dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.bitcast_convert_type(x, to_dtype)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}"
       .format(from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype}
      for from_dtype, to_dtype in itertools.product(
          [np.float32, np.int32, "float32", "int32"], repeat=2)))
  def testBitcastConvertTypeAgainstNumpy(self, from_dtype, to_dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.bitcast_convert_type(x, to_dtype)
    numpy_op = lambda x: lax_reference.bitcast_convert_type(x, to_dtype)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}_weak_type={}"
       .format(from_dtype, to_dtype, weak_type),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "weak_type": weak_type}
      for from_dtype, to_dtype in itertools.product(
          [np.float32, np.int32, "float32", "int32"], repeat=2)
      for weak_type in [True, False]))
  def testBitcastConvertWeakType(self, from_dtype, to_dtype, weak_type):
    rng = jtu.rand_default(self.rng())
    x_in = lax._convert_element_type(rng((2, 3), from_dtype),
                                     weak_type=weak_type)
    op = lambda x: lax.bitcast_convert_type(x, to_dtype)
    self.assertEqual(dtypes.is_weakly_typed(x_in), weak_type)
    x_out = op(x_in)
    self.assertEqual(dtypes.is_weakly_typed(x_out), False)
    x_out_jit = api.jit(op)(x_in)
    self.assertEqual(dtypes.is_weakly_typed(x_out_jit), False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_min_shape={}_operand_shape={}_max_shape={}".format(
          jtu.format_shape_dtype_string(min_shape, dtype),
          jtu.format_shape_dtype_string(operand_shape, dtype),
          jtu.format_shape_dtype_string(max_shape, dtype)),
       "min_shape": min_shape, "operand_shape": operand_shape,
       "max_shape": max_shape, "dtype": dtype}
      for min_shape, operand_shape, max_shape in [
          [(), (2, 3), ()],
          [(2, 3), (2, 3), ()],
          [(), (2, 3), (2, 3)],
          [(2, 3), (2, 3), (2, 3)],
      ]
      for dtype in default_dtypes))
  def testClamp(self, min_shape, operand_shape, max_shape, dtype):
    rng = jtu.rand_default(self.rng())
    shapes = [min_shape, operand_shape, max_shape]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    self._CompileAndCheck(lax.clamp, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_min_shape={}_operand_shape={}_max_shape={}".format(
          jtu.format_shape_dtype_string(min_shape, dtype),
          jtu.format_shape_dtype_string(operand_shape, dtype),
          jtu.format_shape_dtype_string(max_shape, dtype)),
       "min_shape": min_shape, "operand_shape": operand_shape,
       "max_shape": max_shape, "dtype": dtype}
      for min_shape, operand_shape, max_shape in [
          [(), (2, 3), ()],
          [(2, 3), (2, 3), ()],
          [(), (2, 3), (2, 3)],
          [(2, 3), (2, 3), (2, 3)],
      ]
      for dtype in default_dtypes))
  def testClampAgainstNumpy(self, min_shape, operand_shape, max_shape, dtype):
    rng = jtu.rand_default(self.rng())
    shapes = [min_shape, operand_shape, max_shape]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    self._CheckAgainstNumpy(lax_reference.clamp, lax.clamp, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dim={}_baseshape=[{}]_dtype={}_narrs={}".format(
          dim, ",".join(str(d) for d in base_shape), np.dtype(dtype).name,
          num_arrs),
       "dim": dim, "base_shape": base_shape, "dtype": dtype, "num_arrs": num_arrs}
      for num_arrs in [3]
      for dtype in default_dtypes
      for base_shape in [(4,), (3, 4), (2, 3, 4)]
      for dim in range(len(base_shape))))
  def testConcatenate(self, dim, base_shape, dtype, num_arrs):
    rng = jtu.rand_default(self.rng())
    shapes = [base_shape[:dim] + (size,) + base_shape[dim+1:]
              for size, _ in zip(itertools.cycle([3, 1, 4]), range(num_arrs))]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    op = lambda *args: lax.concatenate(args, dim)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dim={}_baseshape=[{}]_dtype={}_narrs={}".format(
          dim, ",".join(str(d) for d in base_shape), np.dtype(dtype).name,
          num_arrs),
       "dim": dim, "base_shape": base_shape, "dtype": dtype, "num_arrs": num_arrs}
      for num_arrs in [3]
      for dtype in default_dtypes
      for base_shape in [(4,), (3, 4), (2, 3, 4)]
      for dim in range(len(base_shape))))
  def testConcatenateAgainstNumpy(self, dim, base_shape, dtype, num_arrs):
    rng = jtu.rand_default(self.rng())
    shapes = [base_shape[:dim] + (size,) + base_shape[dim+1:]
              for size, _ in zip(itertools.cycle([3, 1, 4]), range(num_arrs))]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    op = lambda *args: lax.concatenate(args, dim)
    numpy_op = lambda *args: lax_reference.concatenate(args, dim)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([2, 3], repeat=3)]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in ["VALID", "SAME"]))
  def testConv(self, lhs_shape, rhs_shape, dtype, strides, padding):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv(lhs, rhs, strides, padding)

    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([2, 3], repeat=3)]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in ["VALID", "SAME"]))
  def testConvAgainstNumpy(self, lhs_shape, rhs_shape, dtype, strides, padding):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    op = lambda lhs, rhs: lax.conv(lhs, rhs, strides, padding)
    numpy_op = lambda lhs, rhs: lax_reference.conv(lhs, rhs, strides, padding)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       "_lhs_dilation={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           strides, padding, lhs_dilation, rhs_dilation),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dilation": lhs_dilation,
       "rhs_dilation": rhs_dilation}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([1, 2, 3], repeat=3)]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in [((0, 0), (0, 0)), ((1, 2), (2, 0))]
      for lhs_dilation, rhs_dilation in itertools.product(
          [(1, 1), (1, 2), (2, 2)], repeat=2)))
  def testConvWithGeneralPadding(self, lhs_shape, rhs_shape, dtype, strides,
                                 padding, lhs_dilation, rhs_dilation):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_with_general_padding(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation)

    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       "_lhs_dilation={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           strides, padding, lhs_dilation, rhs_dilation),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dilation": lhs_dilation,
       "rhs_dilation": rhs_dilation}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([1, 2, 3], repeat=3)]
      for dtype in [np.float32] for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in [((0, 0), (0, 0)), ((1, 2), (2, 0))]
      for lhs_dilation, rhs_dilation in itertools.product(
          [(1, 1), (1, 2), (2, 2)], repeat=2)))
  def testConvWithGeneralPaddingAgainstNumpy(
      self, lhs_shape, rhs_shape, dtype, strides, padding, lhs_dilation,
      rhs_dilation):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_with_general_padding(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation,
          precision=lax.Precision.HIGHEST)

    def numpy_fun(lhs, rhs):
      return lax_reference.conv_with_general_padding(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation)

    self._CheckAgainstNumpy(numpy_fun, fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       "_lhs_dilation={}_rhs_dilation={}"
       "_dims={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           strides, padding, lhs_dilation, rhs_dilation,
           ",".join(dim_nums)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dilation": lhs_dilation,
       "rhs_dilation": rhs_dilation, "dimension_numbers": dim_nums,
       "feature_group_count": feature_group_count,
       "batch_group_count": batch_group_count, "perms": perms}
      for batch_group_count, feature_group_count in [(1, 1), (2, 1), (1, 2)]
      for lhs_shape, rhs_shape in [
          ((b * batch_group_count, i * feature_group_count, 9, w),
           (j * feature_group_count * batch_group_count, i, 4, 5))
          for w in [0, 10]
          for b, i, j in itertools.product([2, 3], repeat=3)]
      for dtype in all_dtypes for strides in [(1, 1), (2, 1)]
      for padding in [((1, 2), (2, 0)), ((10, 8), (7, 13))]
      for lhs_dilation, rhs_dilation in itertools.product(
          [(1, 1), (1, 2), (1, 4)], repeat=2)
      for dim_nums, perms in [
        (("NCHW", "OIHW", "NCHW"), ([0, 1, 2, 3], [0, 1, 2, 3])),
        (("NHWC", "HWIO", "NHWC"), ([0, 2, 3, 1], [2, 3, 1, 0])),
        (("NCHW", "HWIO", "NHWC"), ([0, 1, 2, 3], [2, 3, 1, 0])),
      ]))
  def testConvGeneralDilated(self, lhs_shape, rhs_shape, dtype, strides,
                             padding, lhs_dilation, rhs_dilation,
                             feature_group_count, batch_group_count,
                             dimension_numbers, perms):
    if np.issubdtype(dtype, np.integer) or np.issubdtype(dtype, np.bool_):
      if jtu.device_under_test() == "cpu" and jax.lib.version < (0, 1, 65):
        raise SkipTest("Integer convolution requires jaxlib 0.1.65 or newer on CPU")
      # TODO(b/183565702): Support integer convolutions on CPU/GPU.
      if jtu.device_under_test() == "gpu":
        raise SkipTest("Integer convolution not yet supported on GPU")
    rng = jtu.rand_small(self.rng())
    lhs_perm, rhs_perm = perms  # permute to compatible shapes

    def args_maker():
      return [lax.transpose(rng(lhs_shape, dtype), lhs_perm),
              lax.transpose(rng(rhs_shape, dtype), rhs_perm)]

    def fun(lhs, rhs):
      return lax.conv_general_dilated(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation,
          dimension_numbers, feature_group_count=feature_group_count,
          batch_group_count=batch_group_count)

    self._CompileAndCheck(fun, args_maker)

  def testConvGeneralDilatedPatchesOverlapping1D(self):
    lhs = np.array([[1]], np.float32).reshape((1, 1))
    patches = lax.conv_general_dilated_patches(
      lhs=lhs,
      filter_shape=(),
      window_strides=(),
      padding='SAME'
    )
    self.assertAllClose(lhs, patches)

    dn = ('NHC', 'OIH', 'NHC')
    lhs = np.array([1, 2, 3, 4, 5], np.float32).reshape((1, -1, 1))

    patches = lax.conv_general_dilated_patches(
        lhs=lhs,
        filter_shape=(2,),
        window_strides=(2,),
        padding='VALID',
        dimension_numbers=dn
    )
    self.assertAllClose(
        np.array([[1, 2],
                  [3, 4]], np.float32).reshape((1, 2, 2)), patches)

    patches = lax.conv_general_dilated_patches(
        lhs=lhs,
        filter_shape=(3,),
        window_strides=(1,),
        padding='SAME',
        dimension_numbers=dn
    )
    self.assertAllClose(
        np.array([[0, 1, 2],
                  [1, 2, 3],
                  [2, 3, 4],
                  [3, 4, 5],
                  [4, 5, 0]], np.float32).reshape((1, 5, 3)), patches)

    patches = lax.conv_general_dilated_patches(
        lhs=lhs,
        filter_shape=(3,),
        window_strides=(1,),
        padding='SAME',
        rhs_dilation=(2,),
        dimension_numbers=dn
    )
    self.assertAllClose(
        np.array([[0, 1, 3],
                  [0, 2, 4],
                  [1, 3, 5],
                  [2, 4, 0],
                  [3, 5, 0]], np.float32).reshape((1, 5, 3)), patches)

  def testConvGeneralDilatedPatchesOverlapping2D(self):
    lhs = np.array([[1, 2, 3],
                    [4, 5, 6]], np.float32).reshape((1, 2, 3, 1))
    patches = lax.conv_general_dilated_patches(
        lhs=lhs,
        filter_shape=(2, 2),
        window_strides=(1, 1),
        padding='SAME',
        dimension_numbers=('NHWC', 'OIHW', 'NHWC')
    )
    self.assertAllClose(np.array([[1, 2, 4, 5],
                                  [2, 3, 5, 6],
                                  [3, 0, 6, 0],
                                  [4, 5, 0, 0],
                                  [5, 6, 0, 0],
                                  [6, 0, 0, 0]],
                                 np.float32).reshape((1, 2, 3, 4)), patches)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
           "_lhs_shape={}_filter_shape={}_strides={}_padding={}"
           "_dims={}_precision={}".format(
               jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(filter_shape, dtype),
               strides,
               padding,
               "None" if dim_nums is None else ",".join(dim_nums),
               precision
           ),
       "lhs_shape": lhs_shape,
       "filter_shape": filter_shape,
       "dtype": dtype,
       "strides": strides,
       "padding": padding,
       "dimension_numbers": dim_nums,
       "precision": precision
      }
      for dtype in all_dtypes
      for lhs_shape, filter_shape, strides, padding, dim_nums in [
          ((2, 5), (), (), [], ("NC", "OI", "CN")),
          ((2, 3, 4), (2,), (2,), [(0, 2)], ("CNH", "OHI", "HNC")),
          ((3, 1, 4, 5), (1, 3), (1, 3), [(3, 1), (2, 2)],
           ("NCHW", "OIHW", "NCHW")),
          ((3, 2, 5, 6), (4, 3), (4, 3), [(5, 2), (2, 4)],
           None),
          ((1, 2, 3, 4), (1, 1), (1, 1), [(0, 0), (0, 0)],
           ("NCWH", "OHWI", "CNHW")),
          ((1, 2, 3, 4), (3, 2), (1, 1), [(0, 0), (0, 0)],
           ("CWHN", "HOWI", "NCHW")),
          ((2, 3, 4, 5, 6), (2, 1, 3), (2, 1, 3), [(1, 2), (5, 3), (3, 5)],
           ("NHWDC", "HDIWO", "DCWNH"))
      ]
      for precision in [None,
                        lax.Precision.DEFAULT,
                        lax.Precision.HIGH,
                        lax.Precision.HIGHEST]
      ))
  def testConvGeneralDilatedPatchesNonOverlapping(self,
                                                  lhs_shape,
                                                  filter_shape,
                                                  dtype,
                                                  strides,
                                                  padding,
                                                  dimension_numbers,
                                                  precision):
    if np.issubdtype(dtype, np.integer) or np.issubdtype(dtype, np.bool_):
      if jtu.device_under_test() == "cpu" and jax.lib.version < (0, 1, 65):
        raise SkipTest("Integer convolution requires jaxlib 0.1.65 or newer on CPU")
      # TODO(b/183565702): Support integer convolutions on CPU/GPU.
      if jtu.device_under_test() == "gpu":
        raise SkipTest("Integer convolution not yet supported on GPU")
    rng = jtu.rand_small(self.rng())
    lhs = rng(lhs_shape, dtype)

    if dimension_numbers is None:
      lhs_spec, rhs_spec, out_spec = "NCHW", "OIHW", "NCHW"
    else:
      lhs_spec, rhs_spec, out_spec = dimension_numbers

    filter_spec = ''.join(c for c in rhs_spec if c not in ('I', 'O'))
    patches_spec = out_spec.replace('C', 'C' + filter_spec.lower())

    full_padding = []
    for c in lhs_spec:
      if c in ('N', 'C'):
        full_padding += [(0, 0)]
      else:
        full_padding += [padding[filter_spec.index(c)]]

    lhs_padded = np.pad(lhs, full_padding, 'constant')
    out = lax.transpose(lhs_padded, [lhs_spec.index(c) for c in out_spec])

    patches = lax.conv_general_dilated_patches(
        lhs=lhs,
        filter_shape=filter_shape,
        window_strides=strides,
        padding=padding,
        dimension_numbers=dimension_numbers,
        precision=precision
    )

    source = []

    # Test that output spatial shape is factored into `#patches x patch_size`.
    for c in out_spec:
      out_c = out.shape[out_spec.index(c)]
      patch_c = patches.shape[out_spec.index(c)]

      if c == 'N':
        self.assertEqual(out_c, patch_c)
      elif c == 'C':
        self.assertEqual(out_c * np.prod(filter_shape), patch_c)
      else:
        self.assertEqual(out_c, patch_c * filter_shape[filter_spec.index(c)])

        source += [patches_spec.index(c), patches_spec.index(c.lower())]

    # Test that stacking patches together gives the source image, padded.
    c = out_spec.index('C')
    patches = patches.reshape(patches.shape[:c] +
                              (lhs_shape[lhs_spec.index('C')],) +
                              filter_shape +
                              patches.shape[c + 1:]
                              )
    patches = np.moveaxis(patches, source, range(len(source)))
    for i in range(len(filter_shape)):
      patches = patches.reshape(patches.shape[:i] + (-1,) +
                                patches.shape[2 + i:])
    patches = np.moveaxis(
        patches,
        range(len(filter_shape)),
        [out_spec.index(c) for c in out_spec if c not in ('N', 'C')])
    self.assertAllClose(out, patches)

  # TODO(mattjj): test conv_general_dilated against numpy

  def testConv0DIsDot(self):
    rng = jtu.rand_default(self.rng())
    def args_maker():
      return [rng((10, 5), np.float32), rng((5, 7), np.float32)]
    jnp_fun = partial(lax.conv_general_dilated, window_strides=(),
                      padding='VALID', dimension_numbers=('NC', 'IO', 'NC'))
    self._CompileAndCheck(jnp_fun, args_maker)
    self._CheckAgainstNumpy(np.dot, jnp_fun, args_maker, tol=.1)


  @staticmethod
  def _conv_transpose_via_grad(data, kernel, strides, padding,
                               rhs_dilation=None, dimension_numbers=None):
    """Helper method: calculates conv transpose via grad for testing."""
    assert len(data.shape) == len(kernel.shape)
    nspatial = len(data.shape) - 2
    one = (1,) * nspatial
    rhs_dilation = rhs_dilation or one
    dn = lax.conv_dimension_numbers(data.shape, kernel.shape,
                                    dimension_numbers)
    in_shape = np.take(data.shape, dn.lhs_spec)
    in_sdims = in_shape[2:]
    k_shape = np.take(kernel.shape, dn.rhs_spec)
    k_sdims = k_shape[2:]
    e_k_sdims = [(k-1) * r + 1 for k, r in zip(k_sdims, rhs_dilation)]
    if padding == 'VALID':
      o_sdims = [in_sdims[i]*strides[i] + max(e_k_sdims[i]-strides[i],0)
                 for i in range(nspatial)]
    elif padding == 'SAME':
      o_sdims = [in_sdims[i]*strides[i] for i in range(nspatial)]
    o_shape =  [in_shape[0], k_shape[1]] + o_sdims
    out_spec_inv = [x[0] for x in
                    sorted(enumerate(dn.out_spec), key=lambda x: x[1])]
    o_layout = np.take(np.array(o_shape), out_spec_inv)
    placeholder = np.ones(o_layout, data.dtype)
    conv = lambda x: lax.conv_general_dilated(x, kernel, strides, padding,
                                              one, rhs_dilation, dn)
    _, g = api.vjp(conv, placeholder)
    return g(data)[0]

  @staticmethod
  def _transpose_conv_kernel(data, kernel, dimension_numbers):
    dn = lax.conv_dimension_numbers(data.shape, kernel.shape,
                                    dimension_numbers)
    spatial_axes = np.array(dn.rhs_spec)[2:]
    for axis in spatial_axes:
      kernel = np.flip(kernel, axis)
    kernel = np.swapaxes(kernel, dn.rhs_spec[0], dn.rhs_spec[1])
    return kernel

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding, rhs_dilation),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rhs_dilation": rhs_dilation,
          "dspec": dspec}
      for lhs_shape, rhs_shape in [
          ((b, 9, 10, i), (k, k, j, i))  # NB: i,j flipped in RHS for transpose
          for b, i, j, k in itertools.product([2,3],[2,3],[2,3],[3,4,5])]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1), (2, 2), (3, 3)]
      for padding in ["VALID", "SAME"]
      for dspec in [('NHWC', 'HWIO', 'NHWC'),]
      for rhs_dilation in [None, (2, 2)]))
  @jtu.skip_on_flag("jax_skip_slow_tests", True)
  def testConvTranspose2DT(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rhs_dilation):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    # NB: this test calculates conv_transpose performing identically to the
    # lhs-grad of conv.
    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                rhs_dilation=rhs_dilation,
                                dimension_numbers=dspec,
                                transpose_kernel=True)

    def fun_via_grad(lhs, rhs):
      return self._conv_transpose_via_grad(lhs, rhs, strides, padding,
                                           rhs_dilation=rhs_dilation,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun_via_grad, fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding, rhs_dilation),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rhs_dilation": rhs_dilation,
          "dspec": dspec}
      for lhs_shape, rhs_shape in [
          ((b, 9, 10, i), (k, k, i, j))
          for b, i, j, k in itertools.product([2,3],[2,3],[2,3],[3,4,5])]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1), (2, 2), (3, 3)]
      for padding in ["VALID", "SAME"]
      for dspec in [('NHWC', 'HWIO', 'NHWC'),]
      for rhs_dilation in [None, (2, 2)]))
  @jtu.skip_on_flag("jax_skip_slow_tests", True)
  def testConvTranspose2D(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rhs_dilation):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                rhs_dilation=rhs_dilation,
                                dimension_numbers=dspec,
                                transpose_kernel=False)

    def fun_via_grad(lhs, rhs):
      rhs_t = self._transpose_conv_kernel(lhs, rhs, dimension_numbers=dspec)
      return self._conv_transpose_via_grad(lhs, rhs_t, strides, padding,
                                           rhs_dilation=rhs_dilation,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun_via_grad, fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding, rhs_dilation),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rhs_dilation": rhs_dilation,
          "dspec": dspec}
      for lhs_shape, rhs_shape in [
          ((b, 10, i), (k, i, j))
          for b, i, j, k in itertools.product([2,3],[2,3],[2,3],[3,4,5])]
      for dtype in float_dtypes
      for strides in [(1,), (2,), (3,)]
      for padding in ["VALID", "SAME"]
      for dspec in [('NHC', 'HIO', 'NHC'),]
      for rhs_dilation in [None, (2,)]))
  def testConvTranspose1D(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rhs_dilation):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                dimension_numbers=dspec,
                                rhs_dilation=rhs_dilation,
                                transpose_kernel=False)

    def fun_via_grad(lhs, rhs):
      rhs_t = self._transpose_conv_kernel(lhs, rhs, dimension_numbers=dspec)
      return self._conv_transpose_via_grad(lhs, rhs_t, strides, padding,
                                           rhs_dilation=rhs_dilation,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun_via_grad, fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
        "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_rhs_dilation={}".format(
            jtu.format_shape_dtype_string(lhs_shape, dtype),
            jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding, rhs_dilation),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rhs_dilation": rhs_dilation,
          "dspec": dspec}
      for lhs_shape, rhs_shape in [
          ((b, i), (i, j))
          for b, i, j in itertools.product([2,3],[2,3],[2,3])]
      for dtype in float_dtypes
      for strides in [()]
      for padding in ["VALID", "SAME"]
      for dspec in [('NC', 'IO', 'NC'),]
      for rhs_dilation in [None, ()]))
  def testConvTranspose0D(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rhs_dilation):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                dimension_numbers=dspec,
                                rhs_dilation=rhs_dilation,
                                transpose_kernel=False)

    def fun_via_grad(lhs, rhs):
      rhs_t = self._transpose_conv_kernel(lhs, rhs, dimension_numbers=dspec)
      return self._conv_transpose_via_grad(lhs, rhs_t, strides, padding,
                                           rhs_dilation=rhs_dilation,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun_via_grad, fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_precision={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype),
          precision),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "precision": precision}
      for lhs_shape in [(3,), (4, 3)] for rhs_shape in [(3,), (3, 6)]
      for dtype in all_dtypes
      for precision in [None, lax.Precision.DEFAULT, lax.Precision.HIGH,
                        lax.Precision.HIGHEST,
                        (lax.Precision.DEFAULT, lax.Precision.HIGHEST)]))
  def testDot(self, lhs_shape, rhs_shape, dtype, precision):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    self._CompileAndCheck(partial(lax.dot, precision=precision), args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_preferred_element_type={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype),
          jtu.format_shape_dtype_string((), preferred_element_type)
          ),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype, "preferred_element_type": preferred_element_type
     }
      for lhs_shape in [(3,), (4, 3)] for rhs_shape in [(3,), (3, 6)]
      # We check cases where the preferred type is at least as wide as the input
      # type and where both are either both floating-point or both integral,
      # which are the only supported configurations.
      for dtype, preferred_element_type in [
        (np.float16, np.float16), (np.float16, np.float32), (np.float16, np.float64),
        (dtypes.bfloat16, dtypes.bfloat16), (dtypes.bfloat16, np.float32),
        (dtypes.bfloat16, np.float64), (np.float32, np.float32), (np.float32, np.float64),
        (np.float64, np.float64), (np.int8, np.int8), (np.int8, np.int16), (np.int8, np.int32),
        (np.int8, np.int64), (np.int16, np.int16), (np.int16, np.int32), (np.int16, np.int64),
        (np.int32, np.int32), (np.int32, np.int64), (np.int64, np.int64)]))
  def testDotPreferredElement(self, lhs_shape, rhs_shape, dtype, preferred_element_type):
    if (not config.x64_enabled and
       (dtype == np.float64 or preferred_element_type == np.float64
        or dtype == np.int64 or preferred_element_type == np.int64)):
      raise SkipTest("64-bit mode disabled")
    rng = jtu.rand_default(self.rng())
    x = rng(lhs_shape, dtype)
    y = rng(rhs_shape, dtype)
    # We first compute the dot when both inputs are a lower-precision type and
    # preferred_element_type is a higher-precision type. We then compute results
    # where the inputs are first upcast to the higher-precision type and no
    # `preferred_element_type` is given. We expect the result to be extremely
    # similar given the semantics of `preferred_element_type`.
    result_with_preferred_type = lax.dot(x, y, preferred_element_type=preferred_element_type)
    result_with_upcast_inputs = lax.dot(
      x.astype(preferred_element_type),
      y.astype(preferred_element_type))
    self.assertArraysAllClose(result_with_preferred_type, result_with_upcast_inputs)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype}
      for lhs_shape in [(3,), (4, 3)] for rhs_shape in [(3,), (3, 6)]
      for dtype in all_dtypes))
  def testDotAgainstNumpy(self, lhs_shape, rhs_shape, dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    tol = {
      np.float16: 1e-2,
      np.float64: max(jtu.default_tolerance()[np.dtype(np.float64)], 1e-14),
      np.complex128: max(jtu.default_tolerance()[np.dtype(np.complex128)],
                          1e-14)
    }
    lax_op = partial(lax.dot, precision=lax.Precision.HIGHEST)
    self._CheckAgainstNumpy(lax_reference.dot, lax_op, args_maker, tol=tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_lhs_contracting={}_rhs_contracting={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               lhs_contracting, rhs_contracting),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "lhs_contracting": lhs_contracting, "rhs_contracting": rhs_contracting}
      for lhs_shape, rhs_shape, lhs_contracting, rhs_contracting in [
          [(5,), (5,), [0], [0]],
          [(5, 7), (5,), [0], [0]],
          [(7, 5), (5,), [1], [0]],
          [(3, 5), (2, 5), [1], [1]],
          [(5, 3), (5, 2), [0], [0]],
          [(5, 3, 2), (5, 2, 4), [0], [0]],
          [(5, 3, 2), (5, 2, 4), [0,2], [0,1]],
          [(5, 3, 2), (3, 5, 2, 4), [0,2], [1,2]],
          [(1, 2, 2, 3), (1, 2, 3, 1), [1], [1]],
          [(3, 2), (2, 4), [1], [0]],
      ]
      for dtype in all_dtypes))
  def testDotGeneralContractOnly(self, lhs_shape, rhs_shape, dtype,
                                 lhs_contracting, rhs_contracting):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    dimension_numbers = ((lhs_contracting, rhs_contracting), ([], []))

    def fun(lhs, rhs):
      return lax.dot_general(lhs, rhs, dimension_numbers)

    self._CompileAndCheck(fun, args_maker, check_dtypes=False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_dimension_numbers={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               dimension_numbers),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "dimension_numbers": dimension_numbers}
      for lhs_shape, rhs_shape, dimension_numbers in [
          ((3, 3, 2), (3, 2, 4), (([2], [1]), ([0], [0]))),
          ((3, 3, 2), (2, 3, 4), (([2], [0]), ([0], [1]))),
          ((3, 4, 2, 4), (3, 4, 3, 2), (([2], [3]), ([0, 1], [0, 1]))),
      ]
      for dtype in all_dtypes))
  def testDotGeneralContractAndBatch(self, lhs_shape, rhs_shape, dtype,
                                     dimension_numbers):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.dot_general(lhs, rhs, dimension_numbers)

    self._CompileAndCheck(fun, args_maker, check_dtypes=False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_dimension_numbers={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               dimension_numbers),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "dimension_numbers": dimension_numbers}
      for lhs_shape, rhs_shape, dimension_numbers in [
          ((3, 3, 2), (3, 2, 4), (([2], [1]), ([0], [0]))),
          ((3, 3, 2), (2, 3, 4), (([2], [0]), ([0], [1]))),
          ((3, 4, 2, 4), (3, 4, 3, 2), (([2], [3]), ([0, 1], [0, 1]))),
      ]
      for dtype in all_dtypes))
  def testDotGeneralAgainstNumpy(self, lhs_shape, rhs_shape, dtype,
                                 dimension_numbers):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    op = lambda x, y: lax.dot_general(x, y, dimension_numbers)
    numpy_op = lambda x, y: lax_reference.dot_general(x, y, dimension_numbers)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_dtype={}_broadcast_sizes={}".format(
          shape, np.dtype(dtype).name, broadcast_sizes),
       "shape": shape, "dtype": dtype, "broadcast_sizes": broadcast_sizes}
      for shape in [(), (2, 3)]
      for dtype in default_dtypes
      for broadcast_sizes in [(), (2,), (1, 2)]))
  def testBroadcast(self, shape, dtype, broadcast_sizes):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.broadcast(x, broadcast_sizes)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_broadcast_sizes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), broadcast_sizes),
       "shape": shape, "dtype": dtype, "broadcast_sizes": broadcast_sizes}
      for shape in [(), (2, 3)]
      for dtype in default_dtypes
      for broadcast_sizes in [(), (2,), (1, 2)]))
  def testBroadcastAgainstNumpy(self, shape, dtype, broadcast_sizes):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.broadcast(x, broadcast_sizes)
    numpy_op = lambda x: lax_reference.broadcast(x, broadcast_sizes)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_bcdims={}".format(
          jtu.format_shape_dtype_string(inshape, dtype),
          outshape, broadcast_dimensions),
       "inshape": inshape, "dtype": dtype, "outshape": outshape,
       "dimensions": broadcast_dimensions}
      for inshape, outshape, broadcast_dimensions in [
          ([2], [2, 2], [0]),
          ([2], [2, 2], [1]),
          ([2], [2, 3], [0]),
          ([], [2, 3], []),
          ([1], [2, 3], [1]),
      ]
      for dtype in default_dtypes))
  def testBroadcastInDim(self, inshape, dtype, outshape, dimensions):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(inshape, dtype)]
    op = lambda x: lax.broadcast_in_dim(x, outshape, dimensions)
    self._CompileAndCheck(op, args_maker)

  def testBroadcastInDimOperandShapeTranspose(self):
    # Regression test for https://github.com/google/jax/issues/5276
    def f(x):
      return lax.broadcast_in_dim(x, (2, 3, 4), broadcast_dimensions=(0, 1, 2)).sum()
    def g(x):
      return lax.broadcast_in_dim(x.reshape((3,)), (2, 3, 4), broadcast_dimensions=(1,)).sum()
    x = np.ones((1, 3, 1))
    self.assertArraysEqual(jax.grad(f)(x), jax.grad(g)(x))

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_inshape={}_outshape={}_bcdims={}".format(
      jtu.format_shape_dtype_string(inshape, np.float32),
      outshape, broadcast_dimensions),
      "inshape": inshape, "outshape": outshape,
      "broadcast_dimensions": broadcast_dimensions, "err_msg": err_msg}
    for inshape, outshape, broadcast_dimensions, err_msg in [
      ([2], [2, 2], [0, 1], ('broadcast_dimensions must have length equal to '
                              'operand ndim')),
      ([2, 2], [2], [0, 1], ('target broadcast shape must have equal or higher rank '
                             'to the operand shape')),
      ([2], [2, 3], [2], ('broadcast_in_dim broadcast_dimensions must be a subset of output '
                          'dimensions')),
      ([2], [3], [0], ('operand dimension sizes must either be 1, or be '
                       'equal to their corresponding dimensions in the target broadcast shape')),
      ([2, 2], [2, 2], [1, 0], ('broadcast_dimensions must be strictly increasing')),
    ]))
  def testBroadcastInDimShapeCheck(self, inshape, outshape, broadcast_dimensions, err_msg):
    rng = jtu.rand_default(self.rng())
    x = rng(inshape, np.float32)
    with self.assertRaisesRegex(TypeError, err_msg):
      lax.broadcast_in_dim(x, shape=outshape, broadcast_dimensions=broadcast_dimensions)


  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_bcdims={}".format(
          jtu.format_shape_dtype_string(inshape, dtype),
          outshape, broadcast_dimensions),
       "inshape": inshape, "dtype": dtype, "outshape": outshape,
       "dimensions": broadcast_dimensions}
      for inshape, outshape, broadcast_dimensions in [
          ([2], [2, 2], [0]),
          ([2], [2, 2], [1]),
          ([2], [2, 3], [0]),
          ([], [2, 3], []),
          ([1], [2, 3], [1]),
      ]
      for dtype in default_dtypes))
  def testBroadcastInDimAgainstNumpy(self, inshape, dtype, outshape, dimensions):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(inshape, dtype)]
    op = lambda x: lax.broadcast_in_dim(x, outshape, dimensions)
    numpy_op = lambda x: lax_reference.broadcast_in_dim(x, outshape, dimensions)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_inshape={}_dimensions={}".format(
      jtu.format_shape_dtype_string(inshape, np.float32), dimensions),
      "inshape": inshape, "dimensions": dimensions, "error_type": error_type,
      "err_msg": err_msg}
    for inshape, dimensions, error_type, err_msg in [
      ((1, 2, 3), (0, 0), ValueError, 'dimensions are not unique'),
      ((1, 2, 3), (3,), ValueError, 'axis 3 is out of bounds'),
      ((1, 2, 3), (-4,), ValueError, 'axis -4 is out of bounds'),
      ((1, 2, 3), (1,), ValueError, 'cannot select an axis to squeeze out'),
      ((1, 2, 3), (None,), TypeError, 'cannot be interpreted as an integer'),
    ]))
  def testSqueezeShapeCheck(self, inshape, dimensions, error_type, err_msg):
    rng = jtu.rand_default(self.rng())
    x = rng(inshape, np.float32)
    with self.assertRaisesRegex(error_type, err_msg):
      lax.squeeze(x, dimensions=dimensions)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_dimensions={}".format(
          jtu.format_shape_dtype_string(arg_shape, np.float32), dimensions),
       "arg_shape": arg_shape, "dimensions": dimensions}
      for arg_shape, dimensions in [
          [(1,), (0,)],
          [(1,), (-1,)],
          [(2, 1, 4), (1,)],
          [(2, 1, 3, 1), (1,)],
          [(2, 1, 3, 1), (1, 3)],
          [(2, 1, 3, 1), (3,)],
      ]))
  def testSqueeze(self, arg_shape, dimensions):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(arg_shape, np.float32)]
    op = lambda x: lax.squeeze(x, dimensions)
    numpy_op = lambda x: lax_reference.squeeze(x, dimensions)
    self._CompileAndCheck(op, args_maker)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)
    check_grads(op, args_maker(), 2, ["fwd", "rev"], eps=1.)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          jtu.format_shape_dtype_string(out_shape, dtype)),
       "arg_shape": arg_shape, "out_shape": out_shape, "dtype": dtype}
      for dtype in default_dtypes
      for arg_shape, out_shape in [
          [(3, 4), (12,)], [(2, 1, 4), (8,)], [(2, 2, 4), (2, 8)]
      ]))
  def testReshape(self, arg_shape, out_shape, dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(arg_shape, dtype)]
    op = lambda x: lax.reshape(x, out_shape)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          jtu.format_shape_dtype_string(out_shape, dtype)),
       "arg_shape": arg_shape, "out_shape": out_shape, "dtype": dtype}
      for dtype in default_dtypes
      for arg_shape, out_shape in [
          [(3, 4), (12,)], [(2, 1, 4), (8,)], [(2, 2, 4), (2, 8)]
      ]))
  def testReshapeAgainstNumpy(self, arg_shape, out_shape, dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(arg_shape, dtype)]
    op = lambda x: lax.reshape(x, out_shape)
    numpy_op = lambda x: lax_reference.reshape(x, out_shape)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  def testRoundRoundingMethods(self):
    x = np.array([-2.5, -1.5, -0.5, 0.5, 1.5, 2.5], dtype=np.float32)
    self.assertAllClose(lax.round(x, lax.RoundingMethod.AWAY_FROM_ZERO),
                        np.array([-3, -2, -1, 1, 2, 3], dtype=np.float32))
    self.assertAllClose(lax.round(x, lax.RoundingMethod.TO_NEAREST_EVEN),
                        np.array([-2, -2, 0, 0, 2, 2], dtype=np.float32))

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_pads={}"
       .format(jtu.format_shape_dtype_string(shape, dtype), pads),
       "shape": shape, "dtype": dtype, "pads": pads}
      for dtype in default_dtypes
      for shape, pads in [
          ((0, 2), [(1, 2, 1), (0, 1, 0)]),
          ((2, 3), [(1, 2, 1), (0, 1, 0)]),
          ((2,), [(1, 2, 0)]),
          ((1, 2), [(1, 2, 0), (3, 4, 0)]),
          ((1, 2), [(0, 0, 0), (0, 0, 0)]),
          ((2,), [(1, 2, 3),]),
          ((3, 2), [(1, 2, 1), (3, 4, 2)]),
          ((2,), [(-1, 2, 0),]),
          ((4, 2), [(-1, -2, 0), (1, 2, 0)]),
          ((4, 2), [(-1, 2, 0), (1, 2, 2)]),
          ((5,), [(-1, -2, 2),]),
          ((4, 2), [(-1, -2, 1), (1, 2, 2)])
      ]))
  def testPad(self, shape, dtype, pads):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    fun = lambda operand: lax.pad(operand, np.array(0, dtype), pads)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_pads={}"
       .format(jtu.format_shape_dtype_string(shape, dtype), pads),
       "shape": shape, "dtype": dtype, "pads": pads}
      for shape in [(2, 3)]
      for dtype in default_dtypes
      for pads in [
        [(0, 0, 0), (0, 0, 0)],  # no padding
        [(1, 1, 0), (2, 2, 0)],  # only positive edge padding
        [(1, 2, 1), (0, 1, 0)],  # edge padding and interior padding
        [(0, 0, 0), (-1, -1, 0)],  # negative padding
        [(0, 0, 0), (-2, -2, 4)],  # add big dilation then remove from edges
        [(0, 0, 0), (-2, -3, 1)],  # remove everything in one dimension
      ]))
  def testPadAgainstNumpy(self, shape, dtype, pads):
    rng = jtu.rand_small(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.pad(x, np.array(0, dtype), pads)
    numpy_op = lambda x: lax_reference.pad(x, np.array(0, dtype), pads)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  def testPadErrors(self):
    with self.assertRaisesRegex(ValueError, "padding_config"):
      lax.pad(np.zeros(2), 0., [(0, 1, 0), (0, 1, 0)])
    with self.assertRaisesRegex(ValueError, "padding_config"):
      lax.pad(np.zeros(2), 0., [(0, 1, -1)])

  def testReverse(self):
    rev = api.jit(lambda operand: lax.rev(operand, dimensions))

    dimensions = []
    self.assertAllClose(np.array([0, 1, 2, 3]), rev(np.array([0, 1, 2, 3])),
                        check_dtypes=False)

    dimensions = [0]
    self.assertAllClose(np.array([3, 2, 1]), rev(np.array([1, 2, 3])),
                        check_dtypes=False)

    dimensions = [0, 1]
    self.assertAllClose(np.array([[6, 5, 4], [3, 2, 1]]),
                        rev(np.array([[1, 2, 3], [4, 5, 6]])),
                        check_dtypes=False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_predshape={}_argshapes={}".format(
          jtu.format_shape_dtype_string(pred_shape, np.bool_),
          jtu.format_shape_dtype_string(arg_shape, arg_dtype)),
       "pred_shape": pred_shape, "arg_shape": arg_shape, "arg_dtype": arg_dtype}
      for arg_shape in [(), (3,), (2, 3)]
      for pred_shape in ([(), arg_shape] if arg_shape else [()])
      for arg_dtype in default_dtypes))
  def testSelect(self, pred_shape, arg_shape, arg_dtype):
    rng = jtu.rand_default(self.rng())
    def args_maker():
      return [rng(pred_shape, np.bool_), rng(arg_shape, arg_dtype),
              rng(arg_shape, arg_dtype)]
    return self._CompileAndCheck(lax.select, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_predshape={}_argshapes={}".format(
          jtu.format_shape_dtype_string(pred_shape, np.bool_),
          jtu.format_shape_dtype_string(arg_shape, arg_dtype)),
       "pred_shape": pred_shape, "arg_shape": arg_shape, "arg_dtype": arg_dtype}
      for arg_shape in [(), (3,), (2, 3)]
      for pred_shape in ([(), arg_shape] if arg_shape else [()])
      for arg_dtype in default_dtypes))
  def testSelectAgainstNumpy(self, pred_shape, arg_shape, arg_dtype):
    rng = jtu.rand_default(self.rng())
    def args_maker():
      return [rng(pred_shape, np.bool_), rng(arg_shape, arg_dtype),
              rng(arg_shape, arg_dtype)]
    return self._CheckAgainstNumpy(lax_reference.select, lax.select, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_start_indices={}_limit_indices={}_strides={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, limit_indices, strides),
       "shape": shape, "dtype": dtype, "starts": start_indices,
       "limits": limit_indices, "strides": strides}
      for shape, start_indices, limit_indices, strides in [
        [(3,), (1,), (2,), None],
        [(7,), (4,), (7,), None],
        [(5,), (1,), (5,), (2,)],
        [(8,), (1,), (6,), (2,)],
        [(5, 3), (1, 1), (3, 2), None],
        [(5, 3), (1, 1), (3, 1), None],
        [(7, 5, 3), (4, 0, 1), (7, 1, 3), None],
        [(5, 3), (1, 1), (2, 1), (1, 1)],
        [(5, 3), (1, 1), (5, 3), (2, 1)],
      ]
      for dtype in default_dtypes))
  def testSlice(self, shape, dtype, starts, limits, strides):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.slice(x, starts, limits, strides)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_start_indices={}_limit_indices={}_strides={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, limit_indices, strides),
       "shape": shape, "dtype": dtype, "starts": start_indices,
       "limits": limit_indices, "strides": strides}
      for shape, start_indices, limit_indices, strides in [
        [(3,), (1,), (2,), None],
        [(7,), (4,), (7,), None],
        [(5,), (1,), (5,), (2,)],
        [(8,), (1,), (6,), (2,)],
        [(5, 3), (1, 1), (3, 2), None],
        [(5, 3), (1, 1), (3, 1), None],
        [(7, 5, 3), (4, 0, 1), (7, 1, 3), None],
        [(5, 3), (1, 1), (2, 1), (1, 1)],
        [(5, 3), (1, 1), (5, 3), (2, 1)],
      ]
      for dtype in default_dtypes))
  def testSliceAgainstNumpy(self, shape, dtype, starts, limits, strides):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.slice(x, starts, limits, strides)
    numpy_op = lambda x: lax_reference.slice(x, starts, limits, strides)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_size_indices={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, size_indices),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "size_indices": size_indices}
      for shape, start_indices, size_indices in [
        [(3,), np.array((1,)), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(5, 3), np.array((1, 1)), (3, 1)],
        [(7, 5, 3), np.array((4, 1, 0)), (2, 0, 1)],
      ]
      for dtype in default_dtypes))
  def testDynamicSlice(self, shape, dtype, start_indices, size_indices):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype), np.array(start_indices)]
    op = lambda x, starts: lax.dynamic_slice(x, starts, size_indices)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_size_indices={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, size_indices),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "size_indices": size_indices}
      for shape, start_indices, size_indices in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes))
  def testDynamicSliceAgainstNumpy(self, shape, dtype, start_indices, size_indices):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype), np.array(start_indices)]
    op = lambda x, s: lax.dynamic_slice(x, s, size_indices)
    numpy_op = lambda x, s: lax_reference.dynamic_slice(x, s, size_indices)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  def testDynamicSliceInDim(self):
    # Regression test for mixed type problem in dynamic_slice_in_dim.
    rng = jtu.rand_default(self.rng())
    x = rng((6, 7), np.int32)
    np.testing.assert_equal(lax.dynamic_slice_in_dim(x, 2, 3), x[2:5])

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_update_shape={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, update_shape),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "update_shape": update_shape}
      for shape, start_indices, update_shape in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes))
  def testDynamicUpdateSlice(self, shape, dtype, start_indices, update_shape):
    rng = jtu.rand_default(self.rng())

    def args_maker():
      return [rng(shape, dtype), rng(update_shape, dtype),
              np.array(start_indices)]

    self._CompileAndCheck(lax.dynamic_update_slice, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_update_shape={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, update_shape),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "update_shape": update_shape}
      for shape, start_indices, update_shape in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes))
  def testDynamicUpdateSliceAgainstNumpy(self, shape, dtype, start_indices,
                                         update_shape):
    rng = jtu.rand_default(self.rng())

    def args_maker():
      return [rng(shape, dtype), rng(update_shape, dtype),
              np.array(start_indices)]

    self._CheckAgainstNumpy(lax_reference.dynamic_update_slice,
                            lax.dynamic_update_slice, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_perm={}".format(
          jtu.format_shape_dtype_string(shape, dtype), perm),
       "shape": shape, "dtype": dtype, "perm": perm}
      for shape, perm in [
        [(3, 4), (1, 0)],
        [(3, 4), (0, 1)],
        [(3, 4, 5), (2, 1, 0)],
        [(3, 4, 5), (1, 0, 2)],
      ]
      for dtype in default_dtypes))
  def testTranspose(self, shape, dtype, perm):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.transpose(x, perm)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_perm={}".format(
          jtu.format_shape_dtype_string(shape, dtype), perm),
       "shape": shape, "dtype": dtype, "perm": perm}
      for shape, perm in [
        [(3, 4), (1, 0)],
        [(3, 4), (0, 1)],
        [(3, 4, 5), (2, 1, 0)],
        [(3, 4, 5), (1, 0, 2)],
      ]
      for dtype in default_dtypes))
  def testTransposeAgainstNumpy(self, shape, dtype, perm):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.transpose(x, perm)
    numpy_op = lambda x: lax_reference.transpose(x, perm)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_inshape={}_reducedims={}_initval={}"
       .format(op.__name__, jtu.format_shape_dtype_string(shape, dtype), dims,
               init_val),
       "op": op, "init_val": init_val, "shape": shape, "dtype": dtype, "dims": dims}
      for init_val, op, types in [
          (0, lax.add, default_dtypes),
          (1, lax.mul, default_dtypes),
          (0, lax.max, all_dtypes), # non-monoidal
          (-np.inf, lax.max, float_dtypes),
          (dtypes.iinfo(np.int32).min, lax.max, [np.int32]),
          (dtypes.iinfo(np.int64).min, lax.max, [np.int64]),
          (np.inf, lax.min, float_dtypes),
          (dtypes.iinfo(np.int32).max, lax.min, [np.int32]),
          (dtypes.iinfo(np.int64).max, lax.min, [np.int64]),
          (dtypes.iinfo(np.uint32).max, lax.min, [np.uint32]),
          (dtypes.iinfo(np.uint64).max, lax.min, [np.uint64]),
      ]
      for dtype in types
      for shape, dims in [
          [(3, 4, 5), (0,)], [(3, 4, 5), (1, 2)],
          [(3, 4, 5), (0, 2)], [(3, 4, 5), (0, 1, 2)]
      ]))
  def testReduce(self, op, init_val, shape, dtype, dims):
    rng_factory = (jtu.rand_default if dtypes.issubdtype(dtype, np.integer)
                   else jtu.rand_small)
    rng = rng_factory(self.rng())
    init_val = np.asarray(init_val, dtype=dtype)
    fun = lambda operand, init_val: lax.reduce(operand, init_val, op, dims)
    args_maker = lambda: [rng(shape, dtype), init_val]
    self._CompileAndCheck(fun, args_maker)

    # we separately test the version that uses a concrete init_val because it
    # can hit different code paths
    fun = lambda operand: lax.reduce(operand, init_val, op, dims)
    args_maker = lambda: [rng(shape, dtype)]
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}.{}_arr_weak_type={}_init_weak_type={}"
       .format(op_namespace.__name__, op, arr_weak_type, init_weak_type),
       "op": op, "op_namespace": op_namespace, "arr_weak_type": arr_weak_type, "init_weak_type": init_weak_type}
      for op in ["add", "mul"]
      for op_namespace in [lax, operator]
      for arr_weak_type in [True, False]
      for init_weak_type in [True, False]))
  def testReduceWeakType(self, op_namespace, op, arr_weak_type, init_weak_type):
    op = getattr(op_namespace, op)
    arr = lax._convert_element_type(np.arange(10), int, weak_type=arr_weak_type)
    init = lax._convert_element_type(1, int, weak_type=init_weak_type)
    fun = lambda arr, init: lax.reduce(arr, init, op, (0,))
    out = fun(arr, init)
    self.assertEqual(dtypes.is_weakly_typed(out), arr_weak_type and init_weak_type)
    out_jit = api.jit(fun)(arr, init)
    self.assertEqual(dtypes.is_weakly_typed(out_jit), arr_weak_type and init_weak_type)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": ("_op={}_shape={}_dims={}_strides={}_padding={}"
                         "_basedilation={}_windowdilation={}")
       .format(op.__name__, jtu.format_shape_dtype_string(shape, dtype),
               dims, strides, padding, base_dilation, window_dilation),
       "op": op, "init_val": init_val, "dtype": dtype, "shape": shape,
       "dims": dims, "strides": strides, "padding": padding,
       "base_dilation": base_dilation, "window_dilation": window_dilation}
      for init_val, op, dtypes in [
          (0, lax.add, [np.float32]),
          (-np.inf, lax.max, [np.float32]),
          (np.inf, lax.min, [np.float32]),
      ]
      for shape, dims, strides, padding, base_dilation, window_dilation in (
        itertools.chain(
          itertools.product(
            [(4, 6)],
            [(2, 1), (1, 2)],
            [(1, 1), (2, 1), (1, 2)],
            ["VALID", "SAME", [(0, 3), (1, 2)]],
            [(1, 1), (2, 3)],
            [(1, 1), (1, 2)]),
          itertools.product(
            [(3, 2, 4, 6)], [(1, 1, 2, 1), (2, 1, 2, 1)],
            [(1, 2, 2, 1), (1, 1, 1, 1)],
            ["VALID", "SAME", [(0, 1), (1, 0), (2, 3), (0, 2)]],
            [(1, 1, 1, 1), (2, 1, 3, 2)],
            [(1, 1, 1, 1), (1, 2, 2, 1)])))
      for dtype in dtypes))
  def testReduceWindow(self, op, init_val, dtype, shape, dims, strides, padding,
                       base_dilation, window_dilation):
    rng = jtu.rand_small(self.rng())
    init_val = np.asarray(init_val, dtype=dtype)

    def fun(operand, init_val):
      return lax.reduce_window(operand, init_val, op, dims, strides, padding,
                               base_dilation, window_dilation)

    def reference_fun(operand, init_val):
      return lax_reference.reduce_window(operand, init_val, op, dims, strides,
                                         padding, base_dilation)

    args_maker = lambda: [rng(shape, dtype), init_val]
    self._CompileAndCheck(fun, args_maker)
    if all(d == 1 for d in window_dilation):
      self._CheckAgainstNumpy(reference_fun, fun, args_maker)

    # we separately test the version that uses a concrete init_val because it
    # can hit different code paths
    def fun(operand):
      return lax.reduce_window(operand, init_val, op, dims, strides, padding,
                               base_dilation, window_dilation)

    args_maker = lambda: [rng(shape, dtype)]
    self._CompileAndCheck(fun, args_maker)

  def testReduceWindowFailures(self):
    def empty_window_test():
      return lax.reduce_window(np.ones((1,)), 0., lax.add, padding='VALID',
                               window_dimensions=(0,), window_strides=(1,))

    def zero_stride_test():
      return lax.reduce_window(np.ones((1,)), 0., lax.add, padding='VALID',
                               window_dimensions=(1,), window_strides=(0,))

    for failure_fun in [empty_window_test, zero_stride_test]:
      with self.assertRaisesRegex(TypeError, "must have every element be"):
        failure_fun()

    with self.assertRaisesRegex(
        ValueError,
        "Invalid return type from reduction function: <class 'list'>\n"
        "Reduction functions should only return an array.\n"
        "Full return value: .*"):
      return lax.reduce_window(
          np.ones((1,)), 0., lambda x, y: [x + y],
          padding='VALID', window_dimensions=(1,), window_strides=(1,))

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": (f"_shape={shape}_windowdimensions={window_dimensions}"
                         f"_basedilation={base_dilation}_windowdilation="
                         f"{window_dilation}"),
       "shape": shape, "window_dimensions": window_dimensions,
       "base_dilation": base_dilation, "window_dilation": window_dilation}
      for shape, window_dimensions, base_dilation, window_dilation in (
        itertools.chain(
          itertools.product(
            [(4, 6)],
            [(1, 1), (3, 4)],
            [(1, 1), (1, 2), (2, 13), (40, 60)],
            [(1, 1), (1, 2), (2, 13), (40, 60)]),
          itertools.product(
            [(3, 2, 4, 6)],
            [(1, 1, 1, 1), (2, 1, 2, 1)],
            [(1, 1, 1, 1), (1, 2, 2, 1), (30, 40, 3, 2)],
            [(1, 1, 1, 1), (1, 2, 2, 1), (30, 40, 3, 2)])))))
  def testReduceWindowShapeDilation(self, shape, window_dimensions,
                                    base_dilation, window_dilation):
    operand, padding, strides = np.ones(shape), 'SAME', (1,) * len(shape)
    result = lax.reduce_window(operand, 0., lax.add, padding=padding,
                               window_strides=strides,
                               window_dimensions=window_dimensions)
    # With a stride of 1 in each direction and a padding of 'SAME', the
    # shape of the input should be equal to the shape of the result according
    # to https://www.tensorflow.org/xla/operation_semantics#reducewindow.
    self.assertEqual(shape, result.shape)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_shape={}_axis={}_reverse={}"
       .format(op.__name__, jtu.format_shape_dtype_string(shape, dtype), axis,
               reverse),
       "op": op, "np_op": np_op, "shape": shape, "dtype": dtype,
       "axis": axis, "reverse": reverse}
      for op, np_op, types in [
          (lax.cumsum, np.cumsum, default_dtypes),
          (lax.cumprod, np.cumprod, default_dtypes),
          (lax.cummax, np.maximum.accumulate, default_dtypes),
          (lax.cummin, np.minimum.accumulate, default_dtypes),
      ]
      for dtype in types
      for shape in [[10], [3, 4, 5]]
      for axis in range(len(shape))
      for reverse in [False, True]))
  def testCumulativeReduce(self, op, np_op, shape, dtype, axis, reverse):
    rng_factory = (jtu.rand_default if dtypes.issubdtype(dtype, np.integer)
                   else jtu.rand_small)
    rng = rng_factory(self.rng())
    fun = partial(op, axis=axis, reverse=reverse)
    def np_fun(x):
      if reverse:
        return np.flip(np_op(np.flip(x, axis), axis=axis, dtype=dtype), axis)
      else:
        return np_op(x, axis=axis, dtype=dtype)
    args_maker = lambda: [rng(shape, dtype)]
    self._CompileAndCheck(fun, args_maker)
    self._CheckAgainstNumpy(np_fun, fun, args_maker)


  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_out_dtype={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          jtu.format_shape_dtype_string(shape, out_dtype)),
       "shape": shape, "dtype": dtype, "out_dtype": out_dtype}
      for shape in [(), (3,), (3, 4)]
      for dtype in float_dtypes
      for out_dtype in float_dtypes))
  def testReducePrecision(self, shape, dtype, out_dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    info = dtypes.finfo(out_dtype)
    fun = lambda x: lax.reduce_precision(x, info.nexp, info.nmant)
    np_fun = lambda x: np.asarray(x).astype(out_dtype).astype(dtype)
    self._CheckAgainstNumpy(np_fun, fun, args_maker)
    self._CompileAndCheck(fun, args_maker)


  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_axis={}_isstable={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axis, is_stable),
       "shape": shape, "dtype": dtype, "axis": axis, "is_stable": is_stable}
      for dtype in all_dtypes
      for shape in [(5,), (5, 7)]
      for axis in [-1, len(shape) - 1]
      for is_stable in [False, True]))
  def testSort(self, shape, dtype, axis, is_stable):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    fun = lambda x: lax.sort(x, dimension=axis, is_stable=is_stable)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_axis={}_isstable={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axis, is_stable),
        "shape": shape, "dtype": dtype, "axis": axis, "is_stable": is_stable}
      for dtype in all_dtypes
      for shape in [(5,), (5, 7)]
      for axis in [-1, len(shape) - 1]
      for is_stable in [False, True]))
  def testSortAgainstNumpy(self, shape, dtype, axis, is_stable):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.sort(x, dimension=axis, is_stable=is_stable)
    def numpy_op(x):
      if is_stable:
        return lax_reference.sort(x, axis, kind='stable')
      else:
        return lax_reference.sort(x, axis)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_keyshape={}_valshape={}_axis={}_isstable={}".format(
          jtu.format_shape_dtype_string(shape, key_dtype),
          jtu.format_shape_dtype_string(shape, val_dtype),
          axis, is_stable),
       "shape": shape, "key_dtype": key_dtype, "val_dtype": val_dtype,
       "axis": axis, "is_stable": is_stable}
      for key_dtype in float_dtypes + complex_dtypes + int_dtypes + uint_dtypes
      for val_dtype in [np.float32, np.int32, np.uint32]
      for shape in [(3,), (5, 3)]
      for axis in [-1, len(shape) - 1]
      for is_stable in [False, True]))
  def testSortKeyVal(self, shape, key_dtype, val_dtype, axis, is_stable):
    if (np.issubdtype(key_dtype, np.complexfloating) and
        jtu.device_under_test() == "cpu"):
      raise SkipTest("Complex-valued sort not implemented")
    rng = jtu.rand_default(self.rng())
    # This test relies on the property that wherever keys are tied, values are
    # too, since we don't guarantee the same ordering of values with equal keys.
    # To avoid that case, we generate unique keys (globally in the key array).
    def args_maker():
      flat_keys = np.arange(prod(shape), dtype=key_dtype)
      keys = self.rng().permutation(flat_keys).reshape(shape)
      values = rng(shape, val_dtype)
      return keys, values

    fun = lambda keys, values: lax.sort_key_val(keys, values, axis, is_stable)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_num_keys={}".format(
          jtu.format_shape_dtype_string(shape, dtype), num_keys),
       "shape": shape, "dtype": dtype, "num_keys": num_keys}
      for dtype in all_dtypes
      for shape in [(3, 5,), (4, 3)]
      for num_keys in range(1, shape[0] + 1)))
  def testSortNumKeys(self, shape, dtype, num_keys):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: [rng(shape, dtype)]
    lax_fun = lambda x: lax.sort(tuple(x), num_keys=num_keys)
    numpy_fun = lambda x: tuple(x[:, np.lexsort(x[:num_keys][::-1])])
    # self._CompileAndCheck(lax_fun, args_maker)
    self._CheckAgainstNumpy(numpy_fun, lax_fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_keyshape={}_valshape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, key_dtype),
          jtu.format_shape_dtype_string(shape, val_dtype),
          axis),
       "shape": shape, "key_dtype": key_dtype, "val_dtype": val_dtype,
       "axis": axis}
      for key_dtype in float_dtypes + complex_dtypes + int_dtypes + uint_dtypes
      for val_dtype in [np.float32, np.int32, np.uint32]
      for shape in [(3,), (5, 3)]
      for axis in [-1, len(shape) - 1]))
  def testSortKeyValAgainstNumpy(self, shape, key_dtype, val_dtype, axis):
    if (np.issubdtype(key_dtype, np.complexfloating) and
        jtu.device_under_test() == "cpu"):
      raise SkipTest("Complex-valued sort not implemented")
    rng = jtu.rand_default(self.rng())
    # This test relies on the property that wherever keys are tied, values are
    # too, since we don't guarantee the same ordering of values with equal keys.
    # To avoid that case, we generate unique keys (globally in the key array).
    def args_maker():
      flat_keys = np.arange(prod(shape), dtype=key_dtype)
      keys = self.rng().permutation(flat_keys).reshape(shape)
      values = rng(shape, val_dtype)
      return keys, values

    op = lambda ks, vs: lax.sort_key_val(ks, vs, axis)
    numpy_op = lambda ks, vs: lax_reference.sort_key_val(ks, vs, axis)
    self._CheckAgainstNumpy(numpy_op, op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_k={}".format(
          jtu.format_shape_dtype_string(shape, dtype), k),
       "shape": shape, "dtype": dtype, "k": k}
      for dtype in [np.float32, np.int32, np.uint32]
      for shape in [(3,), (5, 3)]
      for k in [1, 3]))
  def testTopK(self, shape, dtype, k):
    def args_maker():
      flat_values = np.arange(prod(shape), dtype=dtype)
      values = self.rng().permutation(flat_values).reshape(shape)
      return [values]
    def reference_top_k(x):
      bcast_idxs = np.broadcast_to(np.arange(shape[-1], dtype=np.int32), shape)
      sorted_vals, sorted_idxs = lax_reference.sort_key_val(x, bcast_idxs)
      return sorted_vals[..., :-k-1:-1], sorted_idxs[..., :-k-1:-1]
    op = lambda vs: lax.top_k(vs, k=k)
    self._CheckAgainstNumpy(op, reference_top_k, args_maker)
    self._CompileAndCheck(op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype}
      for lhs_shape, rhs_shape in [((3, 2), (2, 4)),
                                   ((5, 3, 2), (5, 2, 4)),
                                   ((1, 2, 2, 3), (1, 2, 3, 1))]
      for dtype in float_dtypes))
  def testBatchMatMul(self, lhs_shape, rhs_shape, dtype):
    rng = jtu.rand_small(self.rng())
    arg_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    self._CompileAndCheck(lax.batch_matmul, arg_maker)

  def testCollapse(self):

    @api.jit
    def collapse_first_two(x):
      return lax.collapse(x, 0, 2)

    self.assertEqual((6,), collapse_first_two(np.zeros((2, 3))).shape)
    self.assertEqual((6, 4), collapse_first_two(np.zeros((2, 3, 4))).shape)
    self.assertEqual((2, 3, 4),
                     collapse_first_two(np.zeros((1, 2, 3, 4))).shape)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_axes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), idxs, axes),
       "shape": shape, "dtype": dtype, "idxs": idxs, "axes": axes}
      for dtype in all_dtypes
      for shape, idxs, axes in [
          [(3, 4, 5), (np.array([0, 2, 1]),), (0,)],
          [(3, 4, 5), (np.array([-1, -2]),), (0,)],
          [(3, 4, 5), (np.array([0, 2]), np.array([1, 3])), (0, 1)],
          [(3, 4, 5), (np.array([0, 2]), np.array([1, 3])), (0, 2)],
      ]))
  def testIndexTake(self, shape, dtype, idxs, axes):
    rng = jtu.rand_default(self.rng())
    rand_idxs = lambda: tuple(rng(e.shape, e.dtype) for e in idxs)
    args_maker = lambda: [rng(shape, dtype), rand_idxs()]
    fun = lambda src, idxs: lax.index_take(src, idxs, axes)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_dnums={}_slice_sizes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), idxs, dnums,
          slice_sizes),
       "shape": shape, "dtype": dtype, "idxs": idxs, "dnums": dnums,
       "slice_sizes": slice_sizes}
      for dtype in all_dtypes
      for shape, idxs, dnums, slice_sizes in [
          ((5,), np.array([[0], [2]]), lax.GatherDimensionNumbers(
            offset_dims=(), collapsed_slice_dims=(0,), start_index_map=(0,)),
            (1,)),
          ((10,), np.array([[0], [0], [0]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(), start_index_map=(0,)),
            (2,)),
          ((10, 5,), np.array([[0], [2], [1]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(0,), start_index_map=(0,)),
            (1, 3)),
          ((10, 5), np.array([[0, 2], [1, 0]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(0,), start_index_map=(0, 1)),
            (1, 3)),
      ]))
  def testGather(self, shape, dtype, idxs, dnums, slice_sizes):
    rng = jtu.rand_default(self.rng())
    rng_idx = jtu.rand_int(self.rng(), high=max(shape))
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(shape, dtype), rand_idxs()]
    fun = partial(lax.gather, dimension_numbers=dnums, slice_sizes=slice_sizes)
    self._CompileAndCheck(fun, args_maker)

  # These tests are adapted from the corresponding tests in
  # tensorflow/compiler/xla/service/shape_inference_test.cc with slight
  # variations to account for the implicit setting of index_vector_dim in JAX.
  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": f"_{testcase_name}", "operand_shape": operand_shape,
       "start_indices_shape": start_indices_shape,
       "dimension_numbers": lax.GatherDimensionNumbers(
          offset_dims=offset_dims,
          collapsed_slice_dims=collapsed_slice_dims,
          start_index_map=start_index_map),
       "slice_sizes": slice_sizes, "msg": msg}
      for (testcase_name, operand_shape, start_indices_shape, offset_dims,
           collapsed_slice_dims, start_index_map, slice_sizes, msg) in [
        ("NonAscendingWindowIndices", (10, 9, 8, 7, 6), (5, 4, 3, 2, 1),
         (4, 5, 6, 8, 7), (), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "offset_dims in gather op must be sorted"),
        ("RepeatedWindowIndices", (10, 9, 8, 7, 6), (5, 4, 3, 2, 1),
         (4, 5, 6, 7, 7), (), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "offset_dims in gather op must not repeat"),
        ("WindowIndexOutOfBounds", (10, 9, 8, 7, 6), (5, 4, 3, 2, 1),
         (4, 5, 100, 101, 102), (), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "Offset dimension 2 in gather op is out of bounds"),
        ("WindowIndexBarelyOutOfBounds", (10, 9, 8, 7, 6), (5, 4, 3, 2, 1),
         (4, 5, 6, 7, 9), (), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "Offset dimension 4 in gather op is out of bounds"),
        ("MismatchingElidedWindowDims", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (4,), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "All components of the offset index in a gather op must either be a "
         "offset dimension or explicitly collapsed"),
        ("OutOfBoundsWindowToInputMapping", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (0, 1, 2, 3, 19), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "Invalid collapsed_slice_dims set in gather op; valid range is"),
        ("RepeatedWindowToInputMapping", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (0, 1, 2, 3, 3), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "collapsed_slice_dims in gather op must not repeat"),
        ("MismatchingGatherToInputMapping", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (), (0, 1, 2, 3), (10, 9, 8, 7, 6),
         "Gather op has 4 elements in start_index_map and the bound of "
         "dimension index_vector_dim=4 of start_indices is 5. These two "
         "numbers must be equal."),
        ("OutOfBoundsGatherToInputMapping", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (), (0, 1, 2, 3, 7), (10, 9, 8, 7, 6),
         "Invalid start_index_map"),
        ("RepeatedGatherToInputMapping", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (), (0, 1, 2, 3, 3), (10, 9, 8, 7, 6),
         "start_index_map in gather op must not repeat"),
        ("NonAscendingElidedWindowDims", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7, 8), (2, 1), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "collapsed_slice_dims in gather op must be sorted"),
        ("WindowBoundsTooLarge", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7), (2,), (0, 1, 2, 3, 4), (10, 9, 8, 100, 6),
         "Slice size at index 3 in gather op is out of range"),
        ("MismatchingNumberOfWindowBounds", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7), (), (0, 1, 2, 3, 4), (10, 9, 8, 7),
         "Gather op must have one slice size for every input dimension"),
        ("WindowBoundsNot1ForElidedDim", (10, 9, 8, 7, 6), (5, 4, 3, 2, 5),
         (4, 5, 6, 7), (1,), (0, 1, 2, 3, 4), (10, 9, 8, 7, 6),
         "Gather op can only collapse slice dims with bound 1 or 0, but bound "
         "is 9 for index 1 at position 0.")
      ]
  ))
  def testGatherShapeCheckingRule(self, operand_shape, start_indices_shape,
                                  dimension_numbers, slice_sizes, msg):
    operand = np.ones(operand_shape, dtype=np.int32)
    start_indices = np.ones(start_indices_shape, dtype=np.int32)

    with self.assertRaisesRegex(TypeError, msg):
      lax.gather(operand, start_indices, dimension_numbers, slice_sizes)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums}
      for dtype in inexact_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), np.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), np.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), np.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]))
  def testScatterAdd(self, arg_shape, dtype, idxs, update_shape, dnums):
    rng = jtu.rand_default(self.rng())
    rng_idx = jtu.rand_int(self.rng(), high=max(arg_shape))
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter_add, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), np.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), np.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), np.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]))
  def testScatterMin(self, arg_shape, dtype, idxs, update_shape, dnums):
    rng = jtu.rand_default(self.rng())
    rng_idx = jtu.rand_int(self.rng(), high=max(arg_shape))
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter_min, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), np.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), np.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), np.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]))
  def testScatterMax(self, arg_shape, dtype, idxs, update_shape, dnums):
    rng = jtu.rand_default(self.rng())
    rng_idx = jtu.rand_int(self.rng(), high=max(arg_shape))
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter_max, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), np.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), np.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), np.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]))
  def testScatter(self, arg_shape, dtype, idxs, update_shape, dnums):
    rng = jtu.rand_default(self.rng())
    rng_idx = jtu.rand_int(self.rng(), high=max(arg_shape))
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker)

  # These tests are adapted from the corresponding tests in
  # tensorflow/compiler/xla/service/shape_inference_test.cc with slight
  # variations to account for the implicit setting of index_vector_dim in JAX.
  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": f"_{testcase_name}", "operand_shape": operand_shape,
       "scatter_indices": scatter_indices, "update_shape": update_shape,
       "dimension_numbers": lax.ScatterDimensionNumbers(
          update_window_dims=update_window_dims,
          inserted_window_dims=inserted_window_dims,
          scatter_dims_to_operand_dims=scatter_dims_to_operand_dims),
       "msg": msg}
      for (testcase_name, operand_shape, scatter_indices, update_shape,
           update_window_dims, inserted_window_dims,
           scatter_dims_to_operand_dims, msg) in [
              ("ScatterWithUpdatesBiggerThanInput", (64, 48), np.zeros((32, 1)),
               (65, 32), (0,), (1,), (1,), "Bounds of the window dimensions"),
              ("ScatterWithUpdatesBiggerThanInputV2", (64, 48),
               np.zeros((32, 1)), (32, 49), (1,), (0,), (1,),
               "Bounds of the window dimensions"),
              ("ScatterWithUpdatesNotMatchingIndices", (64, 48),
               np.zeros((32, 1)), (64, 31), (0,), (1,), (1,),
               "Bounds of the scatter dimensions"),
              ("ScatterWithUpdatesNotMatchingIndicesV2", (64, 48),
               np.zeros((32, 1)), (31, 48), (1,), (0,), (1,),
               "Bounds of the scatter dimensions"),
              ("ScatterNdWithUpdatesBiggerThanInput", (64, 48),
               np.zeros((10, 9, 8, 7, 1)), (10, 9, 8, 7, 65), (4,), (1,),
               (0,), "Bounds of the window dimensions"),
              ("ScatterNdWithUpdatesNotMatchingIndices", (64, 48),
               np.zeros((10, 9, 8, 7, 1)), (9, 9, 8, 7, 64), (4,), (1,), (0,),
               "Bounds of the scatter dimensions"),
              ("InvalidUpdates", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4, 1),
               (4, 5, 6), (1, 2), (0, 1, 2, 3, 4),
               "Updates tensor must be of rank 7; got 8."),
              ("NonAscendingUpdateWindowDims", (6, 5, 4, 3, 2),
               np.zeros((5, 4, 3, 2, 1)), (10, 9, 8, 7, 6, 5, 4, 3, 2),
               (4, 5, 6, 8, 7), (), (0, 1, 2, 3, 4),
               "update_window_dims in scatter op must be sorted"),
              ("RepeatedUpdateWindowDims", (6, 5, 4, 3, 2),
               np.zeros((5, 4, 3, 2, 1)), (10, 9, 8, 7, 6, 5, 4, 3, 2),
               (4, 5, 6, 7, 7), (), (0, 1, 2, 3, 4),
               "update_window_dims in scatter op must not repeat"),
              ("OutOfBoundsUpdateWindowDims", (6, 5, 4, 3, 2),
               np.zeros((5, 4, 3, 2, 1)), (10, 9, 8, 7, 6, 5, 4, 3, 2),
               (4, 5, 6, 7, 9), (), (0, 1, 2, 3, 4),
               "Invalid update_window_dims set in scatter op"),
              ("NonAscendingInsertedWindowDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (2, 1), (0, 1, 2, 3, 4),
               "inserted_window_dims in scatter op must be sorted"),
              ("RepeatedInsertedWindowDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (1, 1), (0, 1, 2, 3, 4),
               "inserted_window_dims in scatter op must not repeat"),
              ("OutOfBoundsInsertedWindowDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (1, 5), (0, 1, 2, 3, 4),
               "Invalid inserted_window_dims set in scatter op"),
              ("MismatchingScatterDimsToOperandDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (1, 2), (0, 1, 2, 3),
               "Scatter op has 4 elements in scatter_dims_to_operand_dims and "
               "the bound of dimension index_vector_dim=4 of scatter_indices "
               "is 5. These two numbers must be equal"),
              ("OutOfBoundsScatterDimsToOperandDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (1, 2), (0, 1, 2, 3, 10),
               "Invalid scatter_dims_to_operand_dims mapping"),
              ("RepeatedValuesInScatterDimsToOperandDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (1, 2), (0, 1, 2, 2, 3),
               "scatter_dims_to_operand_dims in scatter op must not repeat"),
              ("InsufficientWindowDims", (50, 49, 48, 47, 46),
               np.zeros((10, 9, 8, 7, 5)), (10, 9, 8, 7, 3, 2, 4),
               (4, 5, 6), (1,), (0, 1, 2, 3),
               "Scatter op has window of size 4; doesn't match operand of "
               "rank 5.")
           ]
      ))
  def testScatterShapeCheckingRule(self, operand_shape, scatter_indices,
                                   update_shape, dimension_numbers, msg):

    operand = np.ones(operand_shape, dtype=np.int32)
    updates = np.ones(update_shape, dtype=np.int32)

    with self.assertRaisesRegex(TypeError, msg):
      lax.scatter(operand, scatter_indices, updates, dimension_numbers)

  def testIssue831(self):
    # Tests the DeviceTuple constant handler
    def f(x):
      g = lambda *args: args[1]
      return api.jit(lax.fori_loop, static_argnums=(2,))( 0, 10, g, x)

    api.jit(f)(1.)  # doesn't crash

  def testReshapeWithUnusualShapes(self):
    ans = lax.reshape(np.ones((3,), np.float32), (lax.add(1, 2), 1))
    self.assertAllClose(ans, np.ones((3, 1), np.float32))

    self.assertRaisesRegex(
      TypeError,
      "Shapes must be 1D sequences of concrete values of integer type.*",
      lambda: lax.reshape(np.ones(3,), (np.array([3, 1]),)))

    self.assertRaisesRegex(
      TypeError,
      "Shapes must be 1D sequences of concrete values of integer type.*",
      lambda: lax.reshape(np.ones(3,), (1.5, 2.0)))

  def testDynamicSliceTypeErrors(self):
    self.assertRaisesRegex(
      TypeError,
      "index arguments to dynamic_slice must be integers of the same type",
      lambda: lax.dynamic_slice(np.ones((3, 4), dtype=np.float32),
                                (np.int32(1), np.int16(2)), (2, 2)))

  def testDynamicUpdateSliceTypeErrors(self):
    self.assertRaisesRegex(
      TypeError,
      "index arguments to dynamic_update_slice must be integers of the same "
      "type",
      lambda: lax.dynamic_update_slice(np.ones((3, 4), dtype=np.float32),
                                       np.zeros((2, 2), dtype=np.float32),
                                       (np.int32(1), np.int16(2))))

  def test_tie_in_error(self):
    raise SkipTest("test no longer needed after trivializing tie_in")
    # with core.skipping_checks():
    #   with self.assertRaisesRegex(
    #       TypeError, ".* of type .*tuple.* is not a valid JAX type"):
    #     api.make_jaxpr(lambda x: lax.tie_in((x, x), 1))(1.)

  def test_primitive_jaxtype_error(self):
    with jax.enable_checks(False):
      with self.assertRaisesRegex(
          TypeError, "Argument .* of type .* is not a valid JAX type"):
        lax.add(1, 'hi')

  def test_reduction_with_repeated_axes_error(self):
    with self.assertRaisesRegex(ValueError, "duplicate value in 'axes' .*"):
      lax.reduce(np.arange(3), 0, lax.add, (0, 0))

  def test_population_count_booleans_not_supported(self):
    # https://github.com/google/jax/issues/3886
    msg = "population_count does not accept dtype bool"
    with self.assertRaisesRegex(TypeError, msg):
      lax.population_count(True)

  def test_conv_general_dilated_different_input_ranks_error(self):
    # https://github.com/google/jax/issues/4316
    msg = ("conv_general_dilated lhs and rhs must have the same number of "
           "dimensions")
    dimension_numbers = lax.ConvDimensionNumbers(lhs_spec=(0, 1, 2),
                                                 rhs_spec=(0, 1, 2),
                                                 out_spec=(0, 1, 2))
    kwargs = { 'window_strides': (1,)
             , 'padding': ((0, 0),)
             , 'lhs_dilation': (1,)
             , 'rhs_dilation': (1,)
             , 'dimension_numbers': dimension_numbers
             , 'feature_group_count': 1
             , 'batch_group_count': 1
             , 'precision': None
             }
    lhs, rhs = np.ones((1, 1, 1)), np.ones((1, 1, 1, 1))
    with self.assertRaisesRegex(ValueError, msg):
      lax.conv_general_dilated(lhs, rhs, **kwargs)

  def test_window_strides_dimension_shape_rule(self):
    # https://github.com/google/jax/issues/5087
    msg = ("conv_general_dilated window and window_strides must have "
           "the same number of dimensions")
    lhs = jax.numpy.zeros((1, 1, 3, 3))
    rhs = np.zeros((1, 1, 1, 1))
    with self.assertRaisesRegex(ValueError, msg):
      jax.lax.conv(lhs, rhs, [1], 'SAME')

  def test_reduce_window_scalar_init_value_shape_rule(self):
    # https://github.com/google/jax/issues/4574
    args = { "operand": np.ones((4, 4), dtype=np.int32)
           , "init_value": np.zeros((1,), dtype=np.int32)
           , "computation": lax.max
           , "window_dimensions": (2, 2)
           , "window_strides": (2, 2)
           , "padding": "VALID"
           , "base_dilation": (1, 1)
           , "window_dilation": (1, 1)
           }

    msg = (r"reduce_window expected init_value to be a scalar but init_value "
           r"has shape \(1,\).")
    with self.assertRaisesRegex(TypeError, msg):
      lax.reduce_window(**args)

  def test_reduce_correctly_works_with_pytrees(self):
    operands = {'x': [np.ones(5), np.arange(5)]}
    init_values = {'x': [0., 0]}
    result = lax.reduce(operands, init_values,
                        lambda x, y: tree_util.tree_multimap(lax.add, x, y),
                        [0])
    self.assertDictEqual(result, {'x': [5., 10.]})

  def test_reduce_with_mismatched_pytrees_errors(self):
    operands = {'x': np.ones(5)}
    bad_init_values = {'y': 0.}

    with self.assertRaisesRegex(ValueError, 'Operands must have the same '
                                'tree structure as init_values'):
      lax.reduce(operands, bad_init_values,
                 lambda x, y: dict(x=x['x'] + y['x']), [0])

  def test_reduce_with_nonscalar_inits_errors(self):
    operands = {'x': np.ones(5)}
    bad_init_values = {'x': np.ones(5)}

    with self.assertRaisesRegex(ValueError,
                                'reduce found non-scalar initial value'):
      lax.reduce(operands, bad_init_values,
                 lambda x, y: dict(x=x['x'] + y['x']), [0])

  def test_select_jvp_complexity(self):
    jaxpr = jax.make_jaxpr(lambda x: jax.jvp(lambda x: lax.select(True, x, x),
                                             (x,), (1.,)))(1.)
    self.assertLen(jaxpr.jaxpr.eqns, 2)

  def testRngBitGenerator(self):
    if not config.x64_enabled:
      raise SkipTest("RngBitGenerator requires 64bit key")

    key = np.array((1, 2)).astype(np.uint64)
    def fn(k):
      return lax.rng_bit_generator(
          k, shape=(5, 7), algorithm=lax.RandomAlgorithm.RNG_THREE_FRY)

    out = fn(key)
    out_jit = api.jit(fn)(key)
    self.assertEqual(out[0].shape, (2,))
    self.assertEqual(out[1].shape, (5, 7))
    self.assertArraysEqual(out[0], out_jit[0])
    self.assertArraysEqual(out[1], out_jit[1])

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dtype={}_weak_type={}".format(dtype.__name__, weak_type),
       "dtype": dtype, "weak_type": weak_type}
      for dtype in all_dtypes + python_scalar_types
      for weak_type in [True, False]))
  def test_const(self, dtype, weak_type):
    if dtype in set(python_scalar_types):
      val = dtype(0)
    else:
      val = lax._convert_element_type(0, dtype, weak_type=weak_type)

    const = lax._const(val, 0)
    self.assertEqual(dtypes.result_type(val), dtypes.result_type(const))

class LazyConstantTest(jtu.JaxTestCase):
  def _Check(self, make_const, expected):
    # check casting to ndarray works
    asarray_result = np.asarray(make_const())

    # check passing as an argument works (should hit constant handler)
    zero = np.array(0, expected.dtype)
    argument_result = lax.add(zero, make_const())

    # check looping into a compiled computation works
    jit_result = api.jit(lambda x: lax.add(x, make_const()))(zero)

    # ensure they're all the same
    self.assertAllClose(asarray_result, expected)
    self.assertAllClose(argument_result, expected)
    self.assertAllClose(jit_result, expected)

    # ensure repr doesn't crash
    repr(make_const())

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_{}_fill={}".format(
          jtu.format_shape_dtype_string(shape, dtype) if dtype else shape,
          fill_value),
       "shape": shape, "dtype": dtype, "fill_value": fill_value}
      for dtype in itertools.chain(default_dtypes, [None])
      for shape in [(), (3,), (2, 3), (2, 3, 4), (1001, 1001)]
      for fill_value in [0, 1, np.pi]))
  def testFilledConstant(self, shape, fill_value, dtype):
    make_const = lambda: lax.full(shape, fill_value, dtype)
    expected = np.full(shape, fill_value,
                        dtype or dtypes.result_type(fill_value))
    self._Check(make_const, expected)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_{}_dim={}".format(
          jtu.format_shape_dtype_string(shape, dtype), dimension),
       "shape": shape, "dtype": dtype, "dimension": dimension}
      for dtype in default_dtypes
      for shape in [(), (3,), (2, 3), (2, 3, 4),
                    # TODO(mattjj): re-enable
                    # (1001, 1001), (101, 101, 101),
                    ]
      for dimension in range(len(shape))))
  def testIotaConstant(self, dtype, shape, dimension):
    make_const = lambda: lax.broadcasted_iota(dtype, shape, dimension)

    arr = np.arange(shape[dimension], dtype=dtypes.canonicalize_dtype(dtype))
    singleton_shape = [1] * len(shape)
    singleton_shape[dimension] = shape[dimension]
    expected = np.broadcast_to(arr.reshape(singleton_shape), shape)

    self._Check(make_const, expected)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_{}_axes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axes),
       "shape": shape, "dtype": dtype, "axes": axes}
      for dtype in default_dtypes
      for shape, axes in [
          [(2, 3), (0, 1)],
          [(2, 3, 4), (0, 1)],
          [(2, 3, 4), (0, 2)],
          [(2, 3, 4), (1, 2)],
          [(2, 3, 4), (0, 1, 2)],
          [(2, 3, 4, 2), (0, 1, 2)],
          [(2, 3, 4, 2), (0, 2, 3)],
          [(1001, 1001), (0, 1)],
      ]))
  def testDeltaConstant(self, dtype, shape, axes):
    make_const = lambda: lax._delta(dtype, shape, axes)
    # don't check the asarray case, just assume it's right
    expected = np.asarray(make_const())
    self._Check(make_const, expected)

  def testBroadcastInDim(self):
    arr = lax.full((2, 1), 1.) + 1.
    arr_np = np.full((2, 1), 1.) + 1.
    expected = lax_reference.broadcast_in_dim(arr_np, (2, 1, 3), (0, 2))
    make_const = lambda: lax.broadcast_in_dim(arr, (2, 1, 3), (0, 2))
    self._Check(make_const, expected)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_input_type={}_dtype={}_value={}_jit={}".format(
          input_type.__name__, dtype.__name__, value, jit),
       "input_type": input_type, "dtype": dtype, "value": value, "jit": jit}
      for input_type in [int, float, np.int32, np.float32, np.array]
      for dtype in [np.int32, np.float32]
      for jit in [True, False]
      for value in [0, 1]))
  def testConvertElementReturnType(self, input_type, dtype, value, jit):
    op = lambda x: lax.convert_element_type(x, dtype)
    if jit:
      op = api.jit(op)
    result = op(input_type(value))
    assert isinstance(result, xla.DeviceArray)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dtype_in={}_dtype_out={}".format(
          dtype_in.__name__, dtype_out.__name__),
       "dtype_in": dtype_in, "dtype_out": dtype_out}
      for dtype_in in all_dtypes for dtype_out in all_dtypes))
  @jtu.ignore_warning(category=np.ComplexWarning)
  def testConvertElementTypeAvoidsCopies(self, dtype_in, dtype_out):
    x = _device_put_raw(np.zeros(5, dtype_in))
    self.assertEqual(x.dtype, dtype_in)
    y = lax.convert_element_type(x, dtype_out)
    self.assertEqual(y.dtype, dtype_out)
    if np.dtype(dtype_in) == np.dtype(dtype_out):
      self.assertIs(x.device_buffer, y.device_buffer)
    else:
      self.assertFalse(x.device_buffer is y.device_buffer)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_fn={}_indexdtype={}"
       .format(jax_fn.__name__, np.dtype(index_dtype).name),
       "index_dtype": index_dtype, "jax_fn": jax_fn}
      for index_dtype in jtu.dtypes.all_inexact + jtu.dtypes.boolean
      for jax_fn in [lax.argmin, lax.argmax]))
  def testArgMinMaxIndexDtypeError(self, jax_fn, index_dtype):
    with self.assertRaisesRegex(TypeError,
                                "index_dtype must be an integer type"):
      jax_fn(np.ones((2, 2)), axis=0, index_dtype=index_dtype)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_fn={}_weaktype={}".format(jax_fn.__name__, weak_type),
       "jax_fn": jax_fn, "weak_type": weak_type}
      for jax_fn in [lax.argmin, lax.argmax]
      for weak_type in [True, False]))
  def testArgMinMaxWeakType(self, jax_fn, weak_type):
    op = lambda x: jax_fn(x, axis=0, index_dtype=np.int32)
    x_in = lax._convert_element_type(np.ones((2, 2)), weak_type=weak_type)
    self.assertEqual(dtypes.is_weakly_typed(x_in), weak_type)
    x_out = op(x_in)
    self.assertEqual(dtypes.is_weakly_typed(x_out), False)
    x_out_jit = api.jit(op)(x_in)
    self.assertEqual(dtypes.is_weakly_typed(x_out_jit), False)

  @parameterized.named_parameters(jtu.cases_from_list(
        {"testcase_name": "_{}".format(rec.op),
         "op_name": rec.op, "rec_dtypes": rec.dtypes}
      for rec in LAX_OPS if rec.nargs == 1))
  def testUnaryWeakTypes(self, op_name, rec_dtypes):
    """Test that all lax unary ops propagate weak_type information appropriately."""
    # Find a valid dtype for the function.
    for dtype in [np.float_, np.int_, np.complex_, np.bool_]:
      dtype = dtypes.canonicalize_dtype(dtype)
      if dtype in rec_dtypes:
        py_val = dtype.type(1).item()
        lax_val = lax.full((), py_val, dtype)
        break
    else:
      raise ValueError("no available dtypes")

    op = getattr(lax, op_name)
    py_op = op(py_val)
    lax_op = op(lax_val)

    self.assertAllClose(py_op, lax_op, check_dtypes=True)
    self.assertTrue(py_op.aval.weak_type)
    self.assertFalse(lax_op.aval.weak_type)

  def testCumsumLengthOne(self):
    # regression test for issue 4672
    x = lax.full((1,), 1)
    out = lax.cumsum(x)
    self.assertArraysEqual(out, x)


class LaxNamedShapeTest(jtu.JaxTestCase):

  def test_abstract_eval(self):
    aval1 = core.ShapedArray((2, 3), np.float32, False, {'i': 10})
    out = lax.sin_p.abstract_eval(aval1)
    self.assertEqual(out, aval1)

    aval1 = core.ShapedArray((2, 3), np.float32, False, {'i': 10})
    aval2 = core.ShapedArray((2, 3), np.float32, False, {'j': 5})
    expected = core.ShapedArray((2, 3), np.float32, False, {'i': 10, 'j': 5})
    out = lax.add_p.abstract_eval(aval1, aval2)
    self.assertEqual(out, expected)

  def test_abstract_eval_collective(self):
    with core.extend_axis_env('i', 10, None):
      aval1 = core.ShapedArray((2, 3), np.float32, False, {'i': 10, 'j': 5})
      expected = core.ShapedArray((2, 3), np.float32, False, {'j': 5})
      out, = lax.psum_p.abstract_eval(aval1, axes=('i',), axis_index_groups=None)
      self.assertEqual(out, expected)

if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
