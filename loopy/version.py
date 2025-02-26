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


import re
from importlib import metadata


VERSION_TEXT = metadata.version("loopy")
_match = re.match(r"^([0-9.]+)([a-z0-9]*?)$", VERSION_TEXT)
assert _match is not None
VERSION_STATUS = _match.group(2)
VERSION = tuple(int(nr) for nr in _match.group(1).split("."))


try:
    import islpy.version
except ImportError:
    _islpy_version = "_UNKNOWN_"
else:
    _islpy_version = islpy.version.VERSION_TEXT

try:
    import cgen.version
except ImportError:
    _cgen_version = "_UNKNOWN_"
else:
    _cgen_version = cgen.version.VERSION_TEXT

DATA_MODEL_VERSION = f"{VERSION_TEXT}-islpy{_islpy_version}-cgen{_cgen_version}-v2"


FALLBACK_LANGUAGE_VERSION = (2018, 2)
MOST_RECENT_LANGUAGE_VERSION = (2018, 2)

LOOPY_USE_LANGUAGE_VERSION_2018_2 = (2018, 2)

LANGUAGE_VERSION_SYMBOLS = [
        "LOOPY_USE_LANGUAGE_VERSION_2018_2",
        ]

__doc__ = """

.. currentmodule:: loopy
.. data:: VERSION

    A tuple representing the current version number of loopy, for example
    **(2017, 2, 1)**. Direct comparison of these tuples will always yield
    valid version comparisons.

.. _language-versioning:

Loopy Language Versioning
-------------------------

At version 2018.1, :mod:`loopy` introduced a language versioning scheme to make
it easier to evolve the language while retaining backward compatibility. What
prompted this is the addition of
:attr:`loopy.Options.enforce_variable_access_ordered`, which (despite
its name) serves to enable a new check that helps ensure that all variable
access in a kernel is ordered as intended. Since that has the potential to
break existing programs, kernels now have to declare support for a given
language version to let them take advantage of this check.

As a result, :mod:`loopy` will now issue a warning when a call to
:func:`loopy.make_kernel` does not declare a language version. Such kernels
will (indefinitely) default to language version 2018.2.  If passing a
language version to :func:`make_kernel` is impractical, you may also import
one of the ``LOOPY_USE_LANGUAGE_VERSION_...`` symbols given below using::

    from loopy.version import LOOPY_USE_LANGUAGE_VERSION_2018_2

in the global namespace of the function calling :func:`make_kernel`. If
*lang_version* in that call is not explicitly given, this value will be used.

Language versions will generally reflect the version number of :mod:`loopy` in
which they were introduced, though it is likely that most versions of
:mod:`loopy` do not introduce language incompatibilities. In such
situations, the previous language version number remains. (In fact, we
will work hard to avoid backward-incompatible language changes.)

.. data:: MOST_RECENT_LANGUAGE_VERSION

    A tuple representing the most recent language version number of loopy, for
    example **(2018, 1)**. Direct comparison of these tuples will always
    yield valid version comparisons.


History of Language Versions
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. data:: LOOPY_USE_LANGUAGE_VERSION_2018_2

    ``loopy.Options.ignore_boostable_into`` is turned on by default.

.. data:: LOOPY_USE_LANGUAGE_VERSION_2018_1

    :attr:`loopy.Options.enforce_variable_access_ordered` is turned on by
    default. Unsupported from :mod:`loopy` version 2020.2 onwards.

    (no longer available)

.. data:: LOOPY_USE_LANGUAGE_VERSION_2017_2_1

    Initial legacy language version. Unsupported from :mod:`loopy` version
    2020.2 onwards.

    (no longer available)
"""
