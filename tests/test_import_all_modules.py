"""Test that all hfvg modules can be imported (catch missing dependencies)."""

import importlib
import pkgutil
import pytest


def find_all_modules(package_name: str) -> list[str]:
    """Recursively find all modules in a package."""
    modules = []
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return modules
    
    if hasattr(package, "__path__"):
        for importer, modname, ispkg in pkgutil.walk_packages(
            path=package.__path__,
            prefix=package.__name__ + ".",
            onerror=lambda x: None,
        ):
            modules.append(modname)
    
    return modules


def test_all_hfvg_modules_import():
    """Test: All hfvg modules can be imported without missing dependencies."""
    modules = find_all_modules("hfvg")
    
    # Should find modules
    assert len(modules) > 0, "Should find hfvg modules"
    
    failed_imports = []
    missing_deps = []
    
    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            # Missing dependency
            missing_deps.append((module_name, str(e)))
        except Exception as e:
            # Other import error
            failed_imports.append((module_name, str(e)))
    
    # Report missing dependencies
    if missing_deps:
        msg = "Missing dependencies found:\n"
        for module, error in missing_deps:
            msg += f"  {module}: {error}\n"
        pytest.fail(msg)
    
    # Report other import errors
    if failed_imports:
        msg = "Import errors found:\n"
        for module, error in failed_imports:
            msg += f"  {module}: {error}\n"
        pytest.fail(msg)


def test_pillow_import():
    """Test: Pillow (PIL) is available for ref QC."""
    try:
        from PIL import Image
        # Create a small test image
        img = Image.new('RGB', (100, 100))
        assert img.size == (100, 100)
    except ImportError as e:
        pytest.fail(f"Pillow not installed: {e}")


def test_critical_imports():
    """Test: Critical runtime dependencies are available."""
    critical_modules = [
        "temporalio",
        "aiosqlite",
        "httpx",
        "pydantic",
        "yaml",  # pyyaml
        "PIL",   # pillow
    ]
    
    missing = []
    for module in critical_modules:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    
    if missing:
        pytest.fail(f"Critical dependencies missing: {', '.join(missing)}")
