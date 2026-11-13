"""Install PEP 562 lazy exports on a package module (stdlib only)."""
from __future__ import annotations

import importlib
from types import ModuleType


def install_lazy_exports(namespace: ModuleType) -> None:
    """Attach ``__getattr__`` / ``__dir__`` that read ``_LAZY_EXPORTS`` and ``__all__`` live."""

    package_name = namespace.__name__

    def __getattr__(name: str) -> object:
        lazy_exports = namespace.__dict__.get("_LAZY_EXPORTS")
        if lazy_exports is None:
            raise AttributeError(
                f"module {package_name!r} has no attribute {name!r}"
            )
        spec = lazy_exports.get(name)
        if spec is None:
            raise AttributeError(
                f"module {package_name!r} has no attribute {name!r}"
            )
        module_name, attr_name = spec
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        namespace.__dict__[name] = value
        return value

    def __dir__() -> list[str]:
        all_names = namespace.__dict__.get("__all__", ())
        return sorted(set(namespace.__dict__) | set(all_names))

    namespace.__getattr__ = __getattr__
    namespace.__dir__ = __dir__
