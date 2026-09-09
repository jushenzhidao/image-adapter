"""Unit tests for the AST sandbox scanner (AC-11/12)."""

import pytest

from adapter.errors import SecurityError
from adapter.sandbox import scan_source


def test_sandbox_allows_whitelisted_imports():
    """AC-12: Scripts can import whitelisted stdlib modules."""
    source = """
import json
import base64
from datetime import datetime
"""
    scan_source(source)  # should not raise


def test_sandbox_forbids_non_whitelisted_imports():
    """AC-11: Scripts cannot import modules outside the whitelist."""
    source = "import subprocess"
    with pytest.raises(SecurityError, match="Forbidden import: subprocess"):
        scan_source(source)


def test_sandbox_forbids_exec_eval():
    """Scripts cannot call or even reference exec/eval."""
    source = 'exec("print(1)")'
    with pytest.raises(SecurityError, match="Forbidden name reference: exec"):
        scan_source(source)

    source = 'result = eval("1+1")'
    with pytest.raises(SecurityError, match="Forbidden name reference: eval"):
        scan_source(source)


def test_sandbox_forbids_open():
    """Scripts cannot call or reference open()."""
    source = 'f = open("/etc/passwd")'
    with pytest.raises(SecurityError, match="Forbidden name reference: open"):
        scan_source(source)


def test_sandbox_forbids_dunder_attributes():
    """AC-11: Scripts cannot access dunder attributes."""
    source = "klass = obj.__class__"
    with pytest.raises(SecurityError, match="Forbidden dunder attribute"):
        scan_source(source)


def test_sandbox_allows_subscript():
    """AC-12 correction: Subscript (obj[key]) is allowed."""
    source = """
data = {"key": "value"}
val = data["key"]
"""
    scan_source(source)  # should not raise


def test_sandbox_allows_await():
    """AC-12 correction: Await is allowed (scripts are async)."""
    source = """
async def fetch():
    result = await some_coro()
"""
    scan_source(source)  # should not raise
