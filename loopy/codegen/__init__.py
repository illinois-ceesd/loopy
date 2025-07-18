from __future__ import annotations


__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import logging
from dataclasses import dataclass, replace
from typing import (
    TYPE_CHECKING,
    Any,
)

import constantdict
from typing_extensions import Self

from loopy.typing import not_none


logger = logging.getLogger(__name__)


import islpy  # to help out Sphinx
import islpy as isl
import pytools  # to help out Sphinx
from pytools import ProcessLogger
from pytools.persistent_dict import WriteOncePersistentDict

from loopy.diagnostic import LoopyError, warn
from loopy.kernel.function_interface import CallableKernel, InKernelCallable
from loopy.tools import LoopyKeyBuilder, caches
from loopy.version import DATA_MODEL_VERSION


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pymbolic import Expression

    from loopy.codegen.result import CodeGenerationResult, GeneratedProgram
    from loopy.codegen.tools import CodegenOperationCacheManager
    from loopy.kernel import LoopKernel
    from loopy.library.reduction import ReductionOpFunction
    from loopy.target import ASTType, TargetBase
    from loopy.translation_unit import CallableId, CallablesTable, TranslationUnit
    from loopy.types import LoopyType


__doc__ = """
.. autoclass:: PreambleInfo

.. autoclass:: VectorizationInfo

.. autoclass:: SeenFunction

.. autoclass:: CodeGenerationState

.. autoclass:: TranslationUnitCodeGenerationResult

.. automodule:: loopy.codegen.result

.. automodule:: loopy.codegen.tools

References
^^^^^^^^^^
.. class:: ExpressionNode

    See :class:`pymbolic.primitives.ExpressionNode`.
"""


# {{{ code generation state

class UnvectorizableError(Exception):
    pass


@dataclass(frozen=True)
class VectorizationInfo:
    """
    .. attribute:: iname
    .. attribute:: length
    .. attribute:: space
    """

    iname: str
    length: int


@dataclass(frozen=True)
class SeenFunction:
    """This is used to track functions that emerge late during code generation,
    e.g. C functions to realize arithmetic. No connection with
    :class:`~loopy.kernel.function_interface.InKernelCallable`.

    .. attribute:: name
    .. attribute:: c_name
    .. attribute:: arg_dtypes

        a tuple of arg dtypes

    .. attribute:: result_dtypes

        a tuple of result dtypes
    """
    name: str
    c_name: str
    arg_dtypes: tuple[LoopyType, ...]
    result_dtypes: tuple[LoopyType, ...]


@dataclass(frozen=True)
class CodeGenerationState:
    """
    .. autoattribute:: kernel
    .. autoattribute:: target
    .. autoattribute:: implemented_domain
    .. autoattribute:: implemented_predicates

    .. autoattribute:: seen_dtypes
    .. autoattribute:: seen_functions
    .. attribute:: seen_atomic_dtypes

    .. autoattribute:: var_subst_map

    .. autoattribute:: allow_complex
    .. autoattribute:: vectorization_info
    .. autoattribute:: is_generating_device_code
    .. autoattribute:: gen_program_name
    .. autoattribute:: schedule_index_end
    .. autoattribute:: callables_table
    .. autoattribute:: is_entrypoint
    .. autoattribute:: codegen_cache_manager
    """

    kernel: LoopKernel

    # LoopKernel should not have a target, should use this instead
    target: TargetBase

    implemented_domain: islpy.Set
    """
    The entire implemented domain (as an :class:`islpy.Set`)
    i.e. all constraints that have been enforced so far.
    """

    implemented_predicates: frozenset[Expression]

    # /!\ mutable
    seen_dtypes: set[LoopyType]
    seen_functions: set[SeenFunction]
    seen_atomic_dtypes: set[LoopyType]

    var_subst_map: constantdict.constantdict[str, Expression]
    allow_complex: bool
    callables_table: CallablesTable
    is_entrypoint: bool
    var_name_generator: pytools.UniqueNameGenerator
    is_generating_device_code: bool

    gen_program_name: str

    schedule_index_end: int
    codegen_cache_manager: CodegenOperationCacheManager
    vectorization_info: VectorizationInfo | None = None

    # {{{ copy helpers

    def copy(self, **kwargs: Any) -> Self:
        return replace(self, **kwargs)

    def copy_and_assign(
            self, name: str, value: Expression) -> CodeGenerationState:
        """Make a copy of self with variable *name* fixed to *value*."""
        return self.copy(var_subst_map=self.var_subst_map.set(name, value))

    def copy_and_assign_many(self, assignments) -> CodeGenerationState:
        """Make a copy of self with *assignments* included."""

        return self.copy(var_subst_map=self.var_subst_map.update(assignments))

    # }}}

    @property
    def expression_to_code_mapper(self):
        return self.ast_builder.get_expression_to_code_mapper(self)

    def intersect(self, other: isl.Set):
        new_impl, new_other = isl.align_two(self.implemented_domain, other)
        return self.copy(implemented_domain=new_impl & new_other)

    def fix(self, iname: str, aff: isl.Aff) -> CodeGenerationState:
        new_impl_domain = self.implemented_domain

        impl_space = self.implemented_domain.get_space()
        if iname not in impl_space.get_var_dict():
            new_impl_domain = (new_impl_domain
                    .add_dims(isl.dim_type.set, 1)
                    .set_dim_name(
                        isl.dim_type.set,
                        new_impl_domain.dim(isl.dim_type.set),
                        iname))
            impl_space = new_impl_domain.get_space()

        from loopy.isl_helpers import iname_rel_aff
        iname_plus_lb_aff = iname_rel_aff(impl_space, iname, "==", aff)

        from loopy.symbolic import pw_aff_to_expr
        cns = isl.Constraint.equality_from_aff(iname_plus_lb_aff)
        expr = pw_aff_to_expr(aff)

        new_impl_domain = new_impl_domain.add_constraint(cns)
        return self.copy_and_assign(iname, expr).copy(
                implemented_domain=new_impl_domain)

    def try_vectorized(self,
                what: str,
                func: Callable[[CodeGenerationState],
                    CodeGenerationResult[ASTType] | None]
            ):
        """If *self* is in a vectorizing state (:attr:`vectorization_info` is
        not None), tries to call func (which must be a callable accepting a
        single :class:`CodeGenerationState` argument). If this fails with
        :exc:`UnvectorizableError`, it unrolls the vectorized loop instead.

        *func* should return a :class:`GeneratedCode` instance.

        :returns: :class:`GeneratedCode`
        """

        if self.vectorization_info is None:
            return func(self)

        try:
            return func(self)
        except UnvectorizableError as e:
            warn(self.kernel, "vectorize_failed",
                    "Vectorization of '%s' failed because '%s'"
                    % (what, e))

            return self.unvectorize(func)

    def unvectorize(self,
                func: Callable[[CodeGenerationState],
                    CodeGenerationResult[ASTType] | None],
            ):
        vinf = self.vectorization_info
        assert vinf is not None

        result: list[CodeGenerationResult[ASTType]] = []
        novec_self = self.copy(vectorization_info=None)

        for i in range(vinf.length):
            idx_aff = isl.Aff.zero_on_domain(
                        isl.Space.params_alloc(self.kernel.isl_context, 0)) + i
            new_codegen_state = novec_self.fix(vinf.iname, idx_aff)
            generated = func(new_codegen_state)

            if isinstance(generated, list):
                result.extend(generated)
            elif generated is None:
                pass
            else:
                result.append(generated)

        from loopy.codegen.result import merge_codegen_results
        return merge_codegen_results(self, result)

    @property
    def ast_builder(self):
        if self.is_generating_device_code:
            return self.kernel.target.get_device_ast_builder()
        else:
            return self.kernel.target.get_host_ast_builder()

# }}}


code_gen_cache: WriteOncePersistentDict[
    TranslationUnit,
    CodeGenerationResult[Any]
] = WriteOncePersistentDict(
         "loopy-code-gen-cache-v3-"+DATA_MODEL_VERSION,
         key_builder=LoopyKeyBuilder(),
         safe_sync=False)


caches.append(code_gen_cache)


@dataclass(frozen=True)
class PreambleInfo:
    """
    .. autoattribute:: kernel
    .. autoattribute:: seen_dtypes
    .. autoattribute:: seen_functions
    .. autoattribute:: seen_atomic_dtypes
    """
    kernel: LoopKernel
    seen_dtypes: set[LoopyType]
    seen_functions: set[SeenFunction]
    seen_atomic_dtypes: set[LoopyType]

    # FIXME: This makes all the above redundant. It probably shouldn't be here.
    codegen_state: CodeGenerationState


# {{{ main code generation entrypoint

def generate_code_for_a_single_kernel(
            kernel: LoopKernel,
            callables_table: CallablesTable,
            target: TargetBase,
            is_entrypoint: bool,
        ) -> CodeGenerationResult[Any]:
    """
    :returns: a :class:`CodeGenerationResult`

    :param kernel: An instance of :class:`loopy.LoopKernel`.
    """

    from loopy.kernel import KernelState
    if kernel.state != KernelState.LINEARIZED:
        raise LoopyError("cannot generate code for a kernel that has not been "
                "scheduled")

    codegen_plog = ProcessLogger(logger, f"{kernel.name}: generate code")

    # {{{ examine arg list

    allow_complex = False
    for var in [*kernel.args, *kernel.temporary_variables.values()]:
        if not_none(var.dtype).involves_complex():
            allow_complex = True

    # }}}

    seen_dtypes = set()
    seen_functions = set()
    seen_atomic_dtypes = set()

    initial_implemented_domain = isl.BasicSet.from_params(kernel.assumptions)

    from loopy.codegen.tools import CodegenOperationCacheManager

    codegen_state = CodeGenerationState(
            kernel=kernel,
            target=target,
            implemented_domain=isl.Set.from_basic_set(initial_implemented_domain),
            implemented_predicates=frozenset(),
            seen_dtypes=seen_dtypes,
            seen_functions=seen_functions,
            seen_atomic_dtypes=seen_atomic_dtypes,
            var_subst_map=constantdict.constantdict(),
            allow_complex=allow_complex,
            var_name_generator=kernel.get_var_name_generator(),
            is_generating_device_code=False,
            gen_program_name=(
                target.host_program_name_prefix
                + kernel.name
                + kernel.target.host_program_name_suffix),
            schedule_index_end=len(not_none(kernel.linearization)),
            callables_table=callables_table,
            is_entrypoint=is_entrypoint,
            codegen_cache_manager=CodegenOperationCacheManager.from_kernel(kernel),
            )

    from loopy.codegen.result import generate_host_or_device_program

    codegen_result = generate_host_or_device_program(
            codegen_state,
            schedule_index=0)

    device_code_str = codegen_result.device_code()

    from loopy.check import check_implemented_domains
    assert check_implemented_domains(kernel, codegen_result.implemented_domains,
            device_code_str)

    # {{{ handle preambles

    for arg in kernel.args:
        seen_dtypes.add(arg.dtype)

    for tv in kernel.temporary_variables.values():
        seen_dtypes.add(tv.dtype)

    if kernel.all_inames():
        seen_dtypes.add(kernel.index_dtype)

    preambles = [
        *kernel.preambles, *codegen_result.device_preambles]

    preamble_info = PreambleInfo(
            kernel=kernel,
            seen_dtypes=seen_dtypes,
            seen_functions=seen_functions,
            # a set of LoopyTypes (!)
            seen_atomic_dtypes=seen_atomic_dtypes,
            codegen_state=codegen_state
            )

    for prea_gen in [
            *kernel.preamble_generators,
            *target.get_device_ast_builder().preamble_generators()]:
        preambles.extend(prea_gen(preamble_info))

    codegen_result = codegen_result.copy(device_preambles=preambles)

    # }}}

    # For faster unpickling in the common case when implemented_domains isn't needed.
    from loopy.tools import LazilyUnpicklingDict
    codegen_result = codegen_result.copy(
            implemented_domains=LazilyUnpicklingDict(
                    codegen_result.implemented_domains))

    codegen_plog.done()

    return codegen_result


def diverge_callee_entrypoints(t_unit: TranslationUnit):
    """
    If a :class:`loopy.kernel.function_interface.CallableKernel` is both an
    entrypoint and a callee, then rename the callee.
    """
    from loopy.translation_unit import (
        get_reachable_resolved_callable_ids,
        make_callable_name_generator,
        rename_resolved_functions_in_a_single_kernel,
    )
    callable_ids = get_reachable_resolved_callable_ids(t_unit.callables_table,
                                                       t_unit.entrypoints)

    new_callables: dict[CallableId, InKernelCallable] = {}
    todo_renames: dict[CallableId, str] = {}

    vng = make_callable_name_generator(t_unit.callables_table)

    for clbl_id in callable_ids & t_unit.entrypoints:
        assert isinstance(clbl_id, str)
        todo_renames[clbl_id] = vng(based_on=clbl_id)

    for name, clbl in t_unit.callables_table.items():
        if name in todo_renames:
            name = todo_renames[name]

        if isinstance(clbl, CallableKernel):
            knl = rename_resolved_functions_in_a_single_kernel(clbl.subkernel,
                                                               todo_renames)
            knl = knl.copy(name=name)
            clbl = clbl.copy(subkernel=knl)

        new_callables[name] = clbl

    return t_unit.copy(callables_table=constantdict.constantdict(new_callables))


@dataclass(frozen=True)
class TranslationUnitCodeGenerationResult:
    """
    .. attribute:: host_program

        A mapping from names of entrypoints to their host
        :class:`~loopy.codegen.result.GeneratedProgram`.

    .. attribute:: device_programs

        A list of :class:`~loopy.codegen.result.GeneratedProgram` instances
        intended to run on the compute device.

    .. attribute:: host_preambles
    .. attribute:: device_preambles

    .. automethod:: host_code
    .. automethod:: device_code
    .. automethod:: all_code

    """
    host_programs: Mapping[str, GeneratedProgram]
    device_programs: Sequence[GeneratedProgram]
    host_preambles: Sequence[tuple[int, str]] = ()
    device_preambles: Sequence[tuple[int, str]] = ()

    def host_code(self):
        from loopy.codegen.result import process_preambles
        preamble_codes = process_preambles(getattr(self, "host_preambles", []))

        return (
                "".join(preamble_codes)
                + "\n"
                + "\n\n".join(str(hp.ast)
                              for hp in self.host_programs.values()))

    def device_code(self):
        from loopy.codegen.result import process_preambles
        preamble_codes = process_preambles(getattr(self, "device_preambles", []))

        return (
                "".join(preamble_codes)
                + "\n"
                + "\n\n".join(str(dp.ast) for dp in self.device_programs))

    def all_code(self):
        from loopy.codegen.result import process_preambles
        preamble_codes = process_preambles(
                tuple(getattr(self, "host_preambles", ()))
                +
                tuple(getattr(self, "device_preambles", ()))
                )

        return (
                "".join(preamble_codes)
                + "\n"
                + "\n\n".join(str(dp.ast) for dp in self.device_programs)
                + "\n\n"
                + "\n\n".join(str(hp.ast) for hp in
                    self.host_programs.values()))


def generate_code_v2(t_unit: TranslationUnit) -> CodeGenerationResult[Any]:
    # {{{ cache retrieval

    from loopy import ABORT_ON_CACHE_MISS, CACHING_ENABLED
    from loopy.kernel import LoopKernel
    from loopy.translation_unit import make_program

    if CACHING_ENABLED:
        input_t_unit = t_unit
        try:
            result = code_gen_cache[input_t_unit]
            logger.debug(f"TranslationUnit with entrypoints {t_unit.entrypoints}:"
                          " code generation cache hit")
            return result
        except KeyError:
            logger.debug(f"TranslationUnit with entrypoints {t_unit.entrypoints}:"
                          " code generation cache miss")
            if ABORT_ON_CACHE_MISS:
                raise

    # }}}

    if isinstance(t_unit, LoopKernel):
        t_unit = make_program(t_unit)

    from loopy.kernel import KernelState
    if t_unit.state < KernelState.PREPROCESSED:
        # Note that we cannot have preprocessing separately for everyone.
        # Since, now the preprocessing of each one depends on the other.
        # So we check if any one of the callable kernels are not preprocesses
        # then, we have to do the preprocessing of every other kernel.
        from loopy.preprocess import preprocess_program
        t_unit = preprocess_program(t_unit)

    from loopy.type_inference import infer_unknown_types
    t_unit = infer_unknown_types(t_unit, expect_completion=True)

    if t_unit.state < KernelState.LINEARIZED:
        from loopy.schedule import linearize
        t_unit = linearize(t_unit)

    # Why diverge? Generated code for a non-entrypoint kernel and an entrypoint
    # kernel isn't same for a general loopy target. For example in OpenCL, a
    # kernel callable from host and the one supposed to be callable from device
    # have different function signatures. To generate correct code, each
    # callable should be exclusively an entrypoint or a non-entrypoint kernel.
    t_unit = diverge_callee_entrypoints(t_unit)

    from loopy.check import pre_codegen_checks
    pre_codegen_checks(t_unit)

    host_programs = {}
    device_programs = []
    device_preambles = []
    callee_fdecls = []

    # {{{ collect host/device programs

    for func_id in sorted(key for key, val in t_unit.callables_table.items()
                          if isinstance(val, CallableKernel)):
        cgr = generate_code_for_a_single_kernel(t_unit[func_id],
                                                t_unit.callables_table,
                                                t_unit.target,
                                                func_id in t_unit.entrypoints)
        if func_id in t_unit.entrypoints:
            host_programs[func_id] = cgr.host_program
        else:
            assert len(cgr.device_programs) == 1
            callee_fdecls.append(cgr.device_programs[0].ast.fdecl)

        device_programs.extend(cgr.device_programs)
        device_preambles.extend(cgr.device_preambles)

    # }}}

    # {{{ collect preambles

    for clbl in t_unit.callables_table.values():
        device_preambles.extend(list(clbl.generate_preambles(t_unit.target)))

    # }}}

    # adding the callee fdecls to the device_programs
    device_programs = ([device_programs[0].copy(
            ast=t_unit.target.get_device_ast_builder().ast_module.Collection(
                [*callee_fdecls, device_programs[0].ast])),
            *device_programs[1:]])

    def not_reduction_op(name: str | ReductionOpFunction) -> str:
        assert isinstance(name, str)
        return name

    cgr = TranslationUnitCodeGenerationResult(
            host_programs={
                not_reduction_op(name): prg
                for name, prg in host_programs.items()},
            device_programs=device_programs,
            device_preambles=device_preambles)

    if CACHING_ENABLED:
        code_gen_cache.store_if_not_present(input_t_unit, cgr)

    return cgr


def generate_code(kernel, device=None):
    if device is not None:
        from warnings import warn
        warn("passing 'device' to generate_code() is deprecated",
                DeprecationWarning, stacklevel=2)

    if device is not None:
        from warnings import warn
        warn("generate_code is deprecated and will stop working in 2023. "
                "Call generate_code_v2 instead.", DeprecationWarning, stacklevel=2)

    codegen_result = generate_code_v2(kernel)

    if len(codegen_result.device_programs) > 1:
        raise LoopyError("kernel passed to generate_code yielded multiple "
                "device programs. Use generate_code_v2.")
    if len(codegen_result.host_programs) > 1:
        raise LoopyError("kernel passed to generate_code yielded multiple "
                "host programs. Use generate_code_v2.")

    return codegen_result.device_code(), None

# }}}


# {{{ generate function body

def generate_body(kernel: TranslationUnit):
    codegen_result = generate_code_v2(kernel)

    if len(codegen_result.device_programs) != 1:
        raise LoopyError("generate_body cannot be used on programs "
                "that yield more than one device program")

    dev_prg, = codegen_result.device_programs

    return str(dev_prg.body_ast)

# }}}

# vim: foldmethod=marker
