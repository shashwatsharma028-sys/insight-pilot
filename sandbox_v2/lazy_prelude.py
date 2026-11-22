"""
P4.1 lazy-seaborn prelude.

The V1 prelude (sandbox/executor.py wrap_code_safely) runs `import seaborn as
sns` in every sandbox subprocess. That import costs about 0.5 s (it also
loads scipy), although most analysis code never touches `sns`. This module
derives the V2 prelude from the V1 wrapper's own output, replacing exactly
that one line with LAZY_SEABORN_BLOCK. Everything else is byte-identical,
including the user code.

The lazy block preserves V1's observable behaviour for analysis code:
1. Importing seaborn changes no rcParams and no pandas options (measured,
   seaborn 0.13.2). It does register 12 colormaps (rocket, mako, flare,
   crest, vlag, icefire and their _r variants), which Matplotlib-only code
   may use by name. The block registers those same colormaps from the
   installed seaborn's own cm.py data WITHOUT importing the seaborn package.
   Seaborn registers a colormap only if the name is free, so a later real
   import does not collide.
2. `sns` is a proxy. The first attribute access imports seaborn, binds the
   real module to `sns` and re-applies warnings.filterwarnings('ignore'),
   restoring V1's effective warning precedence (V1 suppresses all warnings
   after its seaborn import).
3. If the colormap data cannot be loaded exactly as expected, the block
   falls back to an eager `import seaborn as sns`, i.e. V1 behaviour.

Known, documented differences (tests pin them): before first use, `sns` is
not a module object (type(sns), `isinstance(sns, ModuleType)` and
`sns is seaborn` differ), and scipy is not pre-loaded (code must import
scipy itself, as it must under V1 to use it by name).

The block runs in the sandbox subprocess, under the same isolation as the
rest of the prelude. The AST safety scan still applies to the generated
code, unchanged.
"""

V1_SEABORN_LINE = "import seaborn as sns\n"
USER_CODE_MARKER = "# ── Agent-generated code starts here"

LAZY_SEABORN_BLOCK = '''# ── V2 lazy seaborn (P4.1): colormaps registered now, package imported on first use of `sns` ──
def _ip_register_seaborn_colormaps():
    import importlib.util as _iu
    import matplotlib as _mpl
    from matplotlib import colors as _colors
    _spec = _iu.find_spec("seaborn")
    _path = _spec.submodule_search_locations[0] + "/cm.py"
    with open(_path, encoding="utf-8") as _f:
        _src = _f.read()
    _head = "from matplotlib import colors\\nfrom seaborn._compat import register_colormap\\n"
    if not _src.startswith(_head):
        raise ImportError("unexpected seaborn/cm.py layout")
    def register_colormap(name, cmap):
        if name not in _mpl.colormaps:
            _mpl.colormaps.register(cmap, name=name)
    exec(compile(_src[len(_head):], _path, "exec"),
         {"__name__": "_ip_seaborn_cm", "colors": _colors, "register_colormap": register_colormap})

class _IPLazySeaborn:
    __slots__ = ()
    def _ip_load(self):
        import importlib as _il
        import warnings as _w
        _mod = _il.import_module("seaborn")
        _w.filterwarnings("ignore")
        if globals().get("sns") is self:
            globals()["sns"] = _mod
        return _mod
    def __getattr__(self, name):
        return getattr(self._ip_load(), name)
    def __dir__(self):
        return dir(self._ip_load())
    def __repr__(self):
        return repr(self._ip_load())

try:
    _ip_register_seaborn_colormaps()
    sns = _IPLazySeaborn()
except Exception:
    import seaborn as sns
del _ip_register_seaborn_colormaps
'''


class PreludeMismatch(RuntimeError):
    """The V1 prelude no longer has the expected shape; the V2 prelude refuses to guess."""


def derive_lazy_wrapper_text(v1_text: str) -> str:
    """Replace V1's seaborn import with the lazy block, in the prelude only (never in user code)."""
    cut = v1_text.find(USER_CODE_MARKER)
    if cut < 0:
        raise PreludeMismatch("user-code marker not found in the V1 wrapper output")
    prelude, rest = v1_text[:cut], v1_text[cut:]
    if prelude.count(V1_SEABORN_LINE) != 1 or not prelude.startswith("\nimport pandas as pd\n"):
        raise PreludeMismatch("the V1 prelude does not contain exactly one `import seaborn as sns` line")
    return prelude.replace(V1_SEABORN_LINE, LAZY_SEABORN_BLOCK, 1) + rest


def wrap_code_lazy_seaborn(user_code: str, csv_path: str, task_id: str) -> str:
    """V1 wrap_code_safely output with the lazy-seaborn prelude (the V1 function is called, not copied)."""
    import sandbox.executor as v1
    return derive_lazy_wrapper_text(v1.wrap_code_safely(user_code, csv_path, task_id))
