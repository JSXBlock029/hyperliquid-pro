"""
Patch for inspect module to add getargspec compatibility
This is needed for older libraries that still use getargspec
"""

import inspect
import warnings
from collections import namedtuple

# Create ArgSpec namedtuple if it doesn't exist
if not hasattr(inspect, 'ArgSpec'):
    ArgSpec = namedtuple('ArgSpec', ['args', 'varargs', 'keywords', 'defaults'])
    inspect.ArgSpec = ArgSpec

def getargspec(func):
    """
    Compatibility wrapper for inspect.getargspec
    Uses getfullargspec and returns in the old format
    """
    warnings.warn(
        "getargspec() is deprecated, use inspect.signature() or inspect.getfullargspec()",
        DeprecationWarning,
        stacklevel=2
    )
    
    spec = inspect.getfullargspec(func)
    return inspect.ArgSpec(
        args=spec.args,
        varargs=spec.varargs,
        keywords=spec.varkw,
        defaults=spec.defaults
    )

# Monkey patch the inspect module
inspect.getargspec = getargspec

print("Inspect module patched successfully!") 