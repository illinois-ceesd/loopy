"""Data used by the kernel object."""

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

from dataclasses import dataclass, replace
from enum import IntEnum
from sys import intern
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    TypeAlias,
    cast,
)

import numpy  # FIXME: imported as numpy to allow sphinx to resolve things
import numpy as np
from typing_extensions import override

from pytools import ImmutableRecord
from pytools.tag import Tag, Taggable, TagT, UniqueTag as UniqueTagBase

from loopy.diagnostic import LoopyError
from loopy.kernel.array import ArrayBase, ArrayDimImplementationTag
from loopy.kernel.instruction import (  # noqa
    Assignment,
    AtomicInit,
    AtomicUpdate,
    CallInstruction,
    CInstruction,
    InstructionBase,
    MemoryOrdering,
    MemoryScope,
    MultiAssignmentBase,
    VarAtomicity,
    make_assignment,
)
from loopy.typing import ShapeType, auto


if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable, Mapping, Sequence

    from pymbolic import ArithmeticExpression, Expression, Variable

    from loopy.types import LoopyType, ToLoopyTypeConvertible


__doc__ = """
.. autofunction:: filter_iname_tags_by_type

.. autoclass:: InameImplementationTag

.. autoclass:: ConcurrentTag

.. autoclass:: UniqueInameTag

.. autoclass:: AxisTag

.. autoclass:: LocalInameTag

.. autoclass:: GroupInameTag

.. autoclass:: VectorizeTag

.. autoclass:: UnrollTag

.. autoclass:: Iname

References
^^^^^^^^^^

.. class:: ToLoopyTypeConvertible

    See :class:`loopy.ToLoopyTypeConvertible`.

.. class:: TagT

    A type variable with a lower bound of :class:`pytools.tag.Tag`.
"""

# This docstring is included in ref_internals. Do not include parts of the public
# interface, e.g. TemporaryVariable, KernelArgument, ArrayArg.


# {{{ utilities

def _names_from_expr(expr: Expression | str | None) -> frozenset[str]:
    from numbers import Number

    from loopy.symbolic import DependencyMapper
    dep_mapper: DependencyMapper[[]] = DependencyMapper()

    from pymbolic.primitives import ExpressionNode
    if isinstance(expr, str):
        return frozenset({expr})
    elif isinstance(expr, ExpressionNode):
        return frozenset(cast("Variable", v).name for v in dep_mapper(expr))
    elif expr is None or isinstance(expr, Number):
        return frozenset()
    else:
        raise ValueError(f"unexpected value of expression-like object: '{expr}'")


def _names_from_dim_tags(
        dim_tags: Sequence[ArrayDimImplementationTag] | None) -> frozenset[str]:
    from loopy.kernel.array import FixedStrideArrayDimTag
    if dim_tags is not None:
        return frozenset({
            name
            for dim_tag in dim_tags
            if isinstance(dim_tag, FixedStrideArrayDimTag)
            for name in _names_from_expr(dim_tag.stride)
            })
    else:
        return frozenset()

# }}}


# {{{ iname tags

def filter_iname_tags_by_type(
            tags: Iterable[Tag],
            tag_type: type[TagT] | tuple[type[TagT], ...],
            max_num: int | None = None,
            min_num: int | None = None,
        ) -> set[TagT]:
    """Return a subset of *tags* that matches type *tag_type*. Raises exception
    if the number of tags found were greater than *max_num* or less than
    *min_num*.

    :arg tags: An iterable of tags.
    :arg tag_type: a subclass of :class:`loopy.kernel.data.InameImplementationTag`.
    :arg max_num: the maximum number of tags expected to be found.
    :arg min_num: the minimum number of tags expected to be found.
    """

    result: set[TagT] = {tag for tag in tags if isinstance(tag, tag_type)}

    def strify_tag_type():
        if isinstance(tag_type, tuple):
            return ", ".join(t.__name__ for t in tag_type)
        else:
            return tag_type.__name__

    if max_num is not None and len(result) > max_num:
        raise LoopyError("cannot have more than {} tags "
                "of type(s): {}".format(max_num, strify_tag_type()))
    if min_num is not None and len(result) < min_num:
        raise LoopyError("must have more than {} tags "
                "of type(s): {}".format(max_num, strify_tag_type()))

    return result


@dataclass(frozen=True)
class InameImplementationTag(UniqueTagBase):
    def __lt__(self, other):
        return self.__hash__() < other.__hash__()

    def update_persistent_hash(self, key_hash, key_builder):
        """Custom hash computation function for use with
        :class:`pytools.persistent_dict.PersistentDict`.
        """

        return key_builder.rec(key_hash, self.key)

    @property
    def key(self) -> Hashable:
        """Return a hashable, comparable value that is used to ensure
        per-instruction uniqueness of this unique iname tag.

        Also used for persistent hash construction.
        """
        return type(self).__name__


class ConcurrentTag(InameImplementationTag):
    pass


class HardwareConcurrentTag(ConcurrentTag):
    pass


class UniqueInameTag(InameImplementationTag):
    pass


@dataclass(frozen=True)
class AxisTag(UniqueInameTag):
    axis: int
    print_name: ClassVar[str]

    @property
    @override
    def key(self) -> tuple[str, int]:
        return (type(self).__name__, self.axis)

    @override
    def __str__(self):
        return f"{self.print_name}.{self.axis}"


class GroupInameTag(HardwareConcurrentTag, AxisTag):
    print_name: ClassVar[str] = "g"


class LocalInameTagBase(HardwareConcurrentTag):
    pass


class LocalInameTag(LocalInameTagBase, AxisTag):
    print_name: ClassVar[str] = "l"


class AutoLocalInameTagBase(LocalInameTagBase):
    @property
    @override
    def key(self):
        return type(self).__name__


class AutoFitLocalInameTag(AutoLocalInameTagBase):
    @override
    def __str__(self):
        return "l.auto"


# {{{ ilp-like

class IlpBaseTag(ConcurrentTag):
    pass


class UnrolledIlpTag(IlpBaseTag):
    @override
    def __str__(self):
        return "ilp.unr"


class LoopedIlpTag(IlpBaseTag):
    @override
    def __str__(self):
        return "ilp.seq"

# }}}


class VectorizeTag(UniqueInameTag, HardwareConcurrentTag):
    @override
    def __str__(self):
        return "vec"


class UnrollTag(InameImplementationTag):
    @override
    def __str__(self):
        return "unr"


@dataclass(frozen=True)
class UnrollHintTag(InameImplementationTag):
    value: int | None = None

    @property
    @override
    def key(self):
        return (type(self).__name__, self.value)

    @override
    def __str__(self):
        if self.value:
            return f"unr_hint.{self.value}"
        else:
            return "unr_hint"


class ForceSequentialTag(InameImplementationTag):
    @override
    def __str__(self):
        return "forceseq"


class InOrderSequentialSequentialTag(InameImplementationTag):
    @override
    def __str__(self):
        return "ord"


ToInameTagConvertible: TypeAlias  = str | Tag | None


def parse_tag(tag: ToInameTagConvertible) -> Tag | None:
    if tag is None:
        return tag

    if isinstance(tag, Tag):
        return tag

    if not isinstance(tag, str):
        raise ValueError("cannot parse tag: %s" % tag)

    if tag == "for":
        return None
    elif tag == "ord":
        return InOrderSequentialSequentialTag()
    elif tag in ["unr"]:
        return UnrollTag()
    elif tag in ["vec"]:
        return VectorizeTag()
    elif tag in ["ilp", "ilp.unr"]:
        return UnrolledIlpTag()
    elif tag == "ilp.seq":
        return LoopedIlpTag()
    elif tag == "unr_hint":
        return UnrollHintTag()
    elif tag.startswith("unr_hint."):
        offset = len("unr_hint.")
        return UnrollHintTag(int(tag[offset:]))
    elif tag.startswith("g."):
        return GroupInameTag(int(tag[2:]))
    elif tag.startswith("l."):
        axis = tag[2:]
        if axis == "auto":
            return AutoFitLocalInameTag()
        else:
            return LocalInameTag(int(axis))
    else:
        raise ValueError("cannot parse tag: %s" % tag)

# }}}


# {{{ memory address space

class AddressSpace(IntEnum):
    """Storage location of a variable.

    .. attribute:: PRIVATE
    .. attribute:: LOCAL
    .. attribute:: GLOBAL
    """

    # These must occur in ascending order of 'globality' so that
    # max(scope) does the right thing.

    PRIVATE = 0
    LOCAL = 1
    GLOBAL = 2

    @classmethod
    def stringify(cls, val: AddressSpace | type[auto]) -> str:
        if val == cls.PRIVATE:
            return "private"
        elif val == cls.LOCAL:
            return "local"
        elif val == cls.GLOBAL:
            return "global"
        elif val is auto:
            return "<auto>"
        else:
            raise ValueError("unexpected value of AddressSpace")

# }}}


# {{{ arguments

class KernelArgument(ImmutableRecord, Taggable):
    """Base class for all argument types.

    .. attribute:: name
    .. attribute:: dtype
    .. attribute:: is_output
    .. attribute:: is_input

    .. automethod:: supporting_names
    """
    name: str
    dtype: LoopyType | None
    is_output: bool
    is_input: bool

    def __init__(self, **kwargs):
        kwargs["name"] = intern(kwargs.pop("name"))

        dtype = kwargs.pop("dtype", None)

        for_atomic = kwargs.pop("for_atomic", False)

        from loopy.types import to_loopy_type
        dtype = to_loopy_type(
                dtype, allow_auto=True, allow_none=True, for_atomic=for_atomic)

        import loopy as lp
        if dtype is lp.auto:
            raise TypeError("dtype may not be lp.auto")

        kwargs["dtype"] = dtype
        kwargs["is_output"] = kwargs.pop("is_output", None)
        kwargs["is_input"] = kwargs.pop("is_input", None)

        ImmutableRecord.__init__(self, **kwargs)

    def supporting_names(self) -> frozenset[str]:
        """'Supporting' names are those that are likely to be required to be
        present for any use of the argument.
        """

        return frozenset()


@dataclass(frozen=True)
class _ArraySeparationInfo:
    """Not user-facing. If an array has been split because an axis
    is tagged with :class:`~loopy.kernel.data.SeparateArrayArrayDimTag`,
    this records the names of the actually present sub-arrays that
    should be used to realize this array.
    """
    sep_axis_indices_set: frozenset[int]
    subarray_names: Mapping[tuple[int, ...], str]


class ArrayArg(ArrayBase, KernelArgument):
    __doc__ = cast("str", ArrayBase.__doc__) + (
        """
        .. attribute:: address_space

            An attribute of :class:`AddressSpace` defining the address
            space in which the array resides.

        .. attribute:: is_output

            An instance of :class:`bool`. If set to *True*, the array is used to
            return information to the caller. If set to *False*, the callee does not
            write to the array during a call.

        .. attribute:: is_input

            An instance of :class:`bool`. If set to *True*, expected to be provided
            by the caller. If *False*, the callee does not depend on the array
            at kernel entry.
        """)

    address_space: AddressSpace

    # _separation_info is not user-facing and hence not documented.
    _separation_info: _ArraySeparationInfo | None

    allowed_extra_kwargs = (
            "address_space",
            "is_output",
            "is_input",
            "tags",
            "_separation_info")

    def __init__(self, *args, **kwargs):
        if "address_space" not in kwargs:
            raise TypeError("'address_space' must be specified")

        kwargs["is_output"] = kwargs.pop("is_output", None)
        kwargs["is_input"] = kwargs.pop("is_input", None)
        kwargs["_separation_info"] = kwargs.pop("_separation_info", None)

        super().__init__(*args, **kwargs)

    min_target_axes = 0
    max_target_axes = 1

    def __str__(self):
        # Don't mention the type of array arg if shape is known
        # FIXME: Why?
        include_typename = self.shape in (None, auto)

        aspace_str = AddressSpace.stringify(self.address_space)

        inout = []
        if self.is_input:
            inout.append("in")
        if self.is_output:
            inout.append("out")
        if not (self.is_input or self.is_output):
            inout.append("neither_in_nor_out?")

        return (
                self.stringify(include_typename=include_typename)
                + " " + "/".join(inout)
                + " aspace: %s" % aspace_str)

    def update_persistent_hash(self, key_hash, key_builder):
        """Custom hash computation function for use with
        :class:`pytools.persistent_dict.PersistentDict`.
        """
        super().update_persistent_hash(key_hash, key_builder)
        key_builder.rec(key_hash, self.address_space)
        key_builder.rec(key_hash, self.is_output)
        key_builder.rec(key_hash, self.is_input)
        key_builder.rec(key_hash, self._separation_info)

    def supporting_names(self) -> frozenset[str]:
        # Do not consider separation info here: The subarrays don't support, they
        # replace this array.
        return (
                _names_from_expr(self.offset)
                | _names_from_dim_tags(self.dim_tags)
                )


# Making this a function prevents incorrect use in isinstance.
# Note: This is *not* deprecated, as it is super-common and
# incrementally more convenient to use than ArrayArg directly.
def GlobalArg(*args, **kwargs) -> ArrayArg:  # noqa: N802
    address_space = kwargs.pop("address_space", None)
    if address_space is not None:
        raise TypeError("may not pass 'address_space' to GlobalArg")
    kwargs["address_space"] = AddressSpace.GLOBAL

    return ArrayArg(*args, **kwargs)


class ConstantArg(ArrayBase, KernelArgument):
    __doc__ = ArrayBase.__doc__

    def __init__(self, *args, **kwargs):
        if kwargs.pop("address_space", AddressSpace.GLOBAL) != AddressSpace.GLOBAL:
            raise LoopyError("'address_space' for ConstantArg must be GLOBAL.")
        super().__init__(*args, **kwargs)

    # Constant Arg cannot be an output
    is_output = False
    is_input = True
    address_space = AddressSpace.GLOBAL

    min_target_axes = 0
    max_target_axes = 1


class ImageArg(ArrayBase, KernelArgument):
    __doc__ = ArrayBase.__doc__

    def __init__(self, *args, **kwargs):
        if kwargs.pop("address_space", AddressSpace.GLOBAL) != AddressSpace.GLOBAL:
            raise LoopyError("'address_space' for ImageArg must be GLOBAL.")
        super().__init__(*args, **kwargs)

    min_target_axes = 1
    max_target_axes = 3

    # ImageArg cannot be an output (for now)
    is_output = False
    is_input = True
    address_space = AddressSpace.GLOBAL

    @property
    def dimensions(self):
        assert self.dim_tags is not None
        return len(self.dim_tags)

    def get_arg_decl(self, ast_builder, name_suffix, shape, dtype, is_written):
        return ast_builder.get_image_arg_decl(self.name + name_suffix, shape,
                self.num_target_axes(), dtype, is_written)

    def supporting_names(self) -> frozenset[str]:
        return (
                _names_from_expr(self.offset)
                | _names_from_dim_tags(self.dim_tags)
                )


class ValueArg(KernelArgument, Taggable):
    def __init__(self,
                name: str,
                dtype: ToLoopyTypeConvertible | None = None,
                approximately: int = 1000,
                is_output: bool = False,
                is_input: bool = True,
                tags: frozenset[Tag] | None = None,
             ) -> None:
        """
        :arg tags: A an instance of or Iterable of instances of
            :class:`pytools.tag.Tag` intended for consumption by an
            application.
        """

        if tags is None:
            tags = frozenset()

        KernelArgument.__init__(self, name=name,
                dtype=dtype,
                approximately=approximately,
                is_output=is_output,
                is_input=is_input,
                tags=tags)

    def __str__(self):
        import loopy as lp
        assert self.dtype is not lp.auto

        type_str = "<auto/runtime>" if self.dtype is None else str(self.dtype)

        return f"{self.name}: ValueArg, type: {type_str}"

    def __repr__(self):
        return "<%s>" % self.__str__()

    def update_persistent_hash(self, key_hash, key_builder):
        """Custom hash computation function for use with
        :class:`pytools.persistent_dict.PersistentDict`.
        """

        key_builder.rec(key_hash, self.name)
        key_builder.rec(key_hash, self.dtype)

    def get_arg_decl(self, ast_builder):
        return ast_builder.get_value_arg_decl(self.name, (),
                self.dtype, False)

# }}}


# {{{ temporary variable

class TemporaryVariable(ArrayBase):
    __doc__ = cast("str", ArrayBase.__doc__) + """
    .. autoattribute:: storage_shape
    .. autoattribute:: base_indices
    .. autoattribute:: address_space
    .. autoattribute:: base_storage
    .. autoattribute:: initializer
    .. autoattribute:: read_only
    .. autoattribute:: _base_storage_access_may_be_aliasing
    """

    storage_shape: ShapeType | None
    base_indices: tuple[Expression, ...] | None
    address_space: AddressSpace | type[auto]
    base_storage: str | None
    """The name of a storage array that is to be used to actually
    hold the data in this temporary, or *None*. If not *None* or the name
    of an existing variable, a variable of this name and appropriate size
    will be created.
    """

    initializer: numpy.ndarray | None
    """*None* or a :class:`numpy.ndarray` of data to be used to initialize the
    array.
    """

    read_only: bool
    """A :class:`bool` indicating whether the variable may be written during
    its lifetime. If *True*, *initializer* must be given.
    """

    _base_storage_access_may_be_aliasing: bool
    """Whether the temporary is used to alias the underlying base storage.
    Defaults to *False*. If *False*, C-based code generators will declare
    the temporary as a ``restrict`` const pointer to the base storage
    memory location. If *True*, the restrict part is omitted on this
    declaration.
    """

    min_target_axes: ClassVar[int] = 0
    max_target_axes: ClassVar[int] = 1

    allowed_extra_kwargs = (
            "storage_shape",
            "base_indices",
            "address_space",
            "base_storage",
            "initializer",
            "read_only",
            "_base_storage_access_may_be_aliasing",
            )

    def __init__(
                self,
                name: str,
                dtype: ToLoopyTypeConvertible = None,
                shape: ShapeType | type[auto] | None = auto,
                address_space: AddressSpace | type[auto] | None = None,
                dim_tags: Sequence[ArrayDimImplementationTag] | None = None,
                offset: Expression | str | None = 0,
                dim_names: tuple[str, ...] | None = None,
                strides: tuple[Expression, ...] | None = None,
                order: str | None = None,

                base_indices: tuple[Expression, ...] | None = None,
                storage_shape: ShapeType | None = None,

                base_storage: str | None = None,
                initializer: np.ndarray | None = None,
                read_only: bool = False,

                _base_storage_access_may_be_aliasing: bool = False,
                **kwargs: Any
            ) -> None:
        """
        :arg dtype: :class:`loopy.auto` or a :class:`numpy.dtype`
        :arg shape: :class:`loopy.auto` or a shape tuple
        :arg base_indices: :class:`loopy.auto` or a tuple of base indices
        """

        if address_space is None:
            address_space = auto

        if initializer is None:
            pass
        elif isinstance(initializer, np.ndarray):
            if offset != 0:
                raise LoopyError(
                        "temporary variable '%s': "
                        "offset must be 0 if initializer specified"
                        % name)

            from loopy.types import NumpyType, to_loopy_type
            if dtype is auto or dtype is None:
                dtype = NumpyType(initializer.dtype)
            elif to_loopy_type(dtype) != to_loopy_type(initializer.dtype):
                raise LoopyError(
                        "temporary variable '%s': "
                        "dtype of initializer does not match "
                        "dtype of array."
                        % name)

            if shape is auto:
                shape = initializer.shape
            else:
                if shape != initializer.shape:
                    raise LoopyError("Shape of '{}' does not match that of the"
                            " initializer.".format(name))
        else:
            raise LoopyError(
                    "temporary variable '%s': "
                    "initializer must be None or a numpy array"
                    % name)

        if order is None:
            order = "C"

        if shape is not None:
            from loopy.kernel.array import _parse_shape_or_strides
            shape = _parse_shape_or_strides(shape)

        if base_indices is None and shape is not auto and shape is not None:
            assert isinstance(shape, tuple)
            base_indices = (0,) * len(shape)

        if not read_only and initializer is not None:
            raise LoopyError(
                    "temporary variable '%s': "
                    "read-write variables with initializer "
                    "are not currently supported "
                    "(did you mean to set read_only=True?)"
                    % name)

        if base_storage is not None and initializer is not None:
            raise LoopyError(
                    "temporary variable '%s': "
                    "base_storage and initializer are "
                    "mutually exclusive"
                    % name)

        if base_storage is None and _base_storage_access_may_be_aliasing:
            raise LoopyError(
                    "temporary variable '%s': "
                    "_base_storage_access_may_be_aliasing option, but no "
                    "base_storage given!"
                    % name)

        ArrayBase.__init__(self, name=intern(name),
                dtype=dtype, shape=shape, strides=strides,
                dim_tags=dim_tags, offset=offset, dim_names=dim_names,
                order=order,
                base_indices=base_indices,
                address_space=address_space,
                storage_shape=storage_shape,
                base_storage=base_storage,
                initializer=initializer,
                read_only=read_only,
                _base_storage_access_may_be_aliasing=(
                    _base_storage_access_may_be_aliasing),
                **kwargs)

    def copy(self, **kwargs: Any) -> TemporaryVariable:
        address_space = kwargs.pop("address_space", None)

        if address_space is not None:
            kwargs["address_space"] = address_space

        return super().copy(**kwargs)

    @property
    def nbytes(self) -> Expression:
        if self.storage_shape is not None:
            shape = self.storage_shape
        else:
            if self.shape is None:
                raise ValueError("shape is None")
            if self.shape is auto:
                raise ValueError("shape is auto")
            shape = cast("tuple[ArithmeticExpression]", self.shape)

        if self.dtype is None:
            raise ValueError("data type is indeterminate")

        from pytools import product
        return product(si for si in shape)*self.dtype.itemsize

    def __str__(self) -> str:
        if self.address_space is auto:
            aspace_str = "auto"
        else:
            aspace_str = AddressSpace.stringify(self.address_space)

        if self.base_storage is None:
            bs_str = ""
        else:
            bs_str = " base_storage: "+str(self.base_storage)

        return (
                self.stringify(include_typename=False)
                + f" aspace: {aspace_str}{bs_str}")

    def __eq__(self, other):
        return (
                super().__eq__(other)
                and self.storage_shape == other.storage_shape
                and self.base_indices == other.base_indices
                and self.address_space == other.address_space
                and self.base_storage == other.base_storage
                and (
                    (self.initializer is None and other.initializer is None)
                    or np.array_equal(self.initializer, other.initializer))
                and self.read_only == other.read_only
                and (self._base_storage_access_may_be_aliasing
                    == other._base_storage_access_may_be_aliasing)
                )

    def update_persistent_hash(self, key_hash, key_builder):
        """Custom hash computation function for use with
        :class:`pytools.persistent_dict.PersistentDict`.
        """

        super().update_persistent_hash(key_hash, key_builder)
        key_builder.rec(key_hash, self.storage_shape)
        key_builder.rec(key_hash, self.base_indices)
        key_builder.rec(key_hash, self.address_space)
        key_builder.rec(key_hash, self.base_storage)

        initializer = self.initializer
        if initializer is not None:
            initializer = (initializer.tolist(), initializer.dtype)
        key_builder.rec(key_hash, initializer)

        key_builder.rec(key_hash, self.read_only)
        key_builder.rec(key_hash, self._base_storage_access_may_be_aliasing)

    def supporting_names(self) -> frozenset[str]:
        return (
                _names_from_expr(self.offset)
                | _names_from_dim_tags(self.dim_tags)
                | (
                    frozenset({self.base_storage})
                    if self.base_storage else frozenset())
                )

# }}}


# {{{ substitution rule

@dataclass(frozen=True)
class SubstitutionRule:
    """
    .. autoattribute:: name
    .. autoattribute:: arguments
    .. autoattribute:: expression
    """

    name: str
    arguments: Sequence[str]
    expression: Expression

    def copy(self, **kwargs: Any) -> SubstitutionRule:
        return replace(self, **kwargs)

    def update_persistent_hash(self, key_hash, key_builder):
        key_builder.rec(key_hash, self.name)
        key_builder.rec(key_hash, self.arguments)
        key_builder.rec(key_hash, self.expression)


# }}}


# {{{ function call mangling

class CallMangleInfo(ImmutableRecord):
    """
    .. attribute:: target_name

        A string. The name of the function to be called in the
        generated target code.

    .. attribute:: result_dtypes

        A tuple of :class:`loopy.types.LoopyType` instances indicating what
        types of values the function returns.

    .. attribute:: arg_dtypes

        A tuple of :class:`loopy.types.LoopyType` instances indicating what
        types of arguments the function actually receives.
    """

    def __init__(self, target_name, result_dtypes, arg_dtypes):
        assert isinstance(result_dtypes, tuple)

        super().__init__(
                target_name=target_name,
                result_dtypes=result_dtypes,
                arg_dtypes=arg_dtypes)

# }}}


# {{{ Iname class

@dataclass(frozen=True)
class Iname(Taggable):
    """
    Records an iname in a :class:`~loopy.LoopKernel`. See :ref:`domain-tree` for
    semantics of *inames* in :mod:`loopy`.

    This class records the metadata attached to an iname as instances of
    :class:pytools.tag.Tag`. A tag maybe a builtin tag like
    :class:`loopy.kernel.data.InameImplementationTag` or a user-defined custom
    tag. Custom tags may be attached to inames to be used in targeting later
    during transformations.

    .. attribute:: name

        An instance of :class:`str`, denoting the iname's name.

    .. attribute:: tags

        An instance of :class:`frozenset` of :class:`pytools.tag.Tag`.
    """
    name: str
    tags: frozenset[Tag]

    def copy(self, **kwargs: Any) -> Iname:
        return replace(self, **kwargs)

    def _with_new_tags(self, tags):
        return self.copy(tags=tags)

# }}}


# vim: foldmethod=marker
