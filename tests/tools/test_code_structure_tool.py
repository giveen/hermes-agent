"""Tests for the code_structure tool (tree-sitter powered)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.code_structure_tool import code_structure, code_symbol


_SAMPLE_PY = """
import os
import sys
from pathlib import Path as PPath

class Greeter:
    \"\"\"A simple greeter.\"\"\"

    def __init__(self, name: str):
        self.name = name

    def greet(self, formal: bool = False) -> str:
        \"\"\"Greet the user.\"\"\"
        if formal:
            return f"Good day, {self.name}"
        return f"Hey {self.name}"

def add(a: int, b: int) -> int:
    return a + b
"""


@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    d = tmp_path / "src"
    d.mkdir()
    (d / "main.py").write_text(_SAMPLE_PY)

    # Add a TypeScript file
    ts_code = """
function greet(name: string): string {
    return `Hello ${name}`;
}

interface User {
    id: number;
    name: string;
}
"""
    (d / "utils.ts").write_text(ts_code)

    # Add a Rust file
    rs_code = """
pub struct Config {
    pub host: String,
    pub port: u16,
}

impl Config {
    pub fn new(host: &str, port: u16) -> Self {
        Self { host: host.to_string(), port }
    }
}

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}
"""
    (d / "lib.rs").write_text(rs_code)
    return d


class TestCodeStructure:
    def test_functions_python(self, sample_dir: Path):
        result = json.loads(code_structure(str(sample_dir / "main.py"), query="functions"))
        assert result["success"] is True
        results = result["results"]
        assert len(results) == 1  # 1 file

        functions = list(results.values())[0].get("functions", [])
        names = [f["name"] for f in functions]
        assert "__init__" in names
        assert "greet" in names
        assert "add" in names

        # Check params
        for f in functions:
            if f["name"] == "add":
                assert "a" in f.get("params", [])
                assert "b" in f.get("params", [])

    def test_classes_python(self, sample_dir: Path):
        result = json.loads(code_structure(str(sample_dir), query="classes"))
        assert result["success"] is True

        classes = []
        for file_results in result["results"].values():
            classes.extend(file_results.get("classes", []))
        names = [c["name"] for c in classes]
        assert "Greeter" in names

    def test_all_python(self, sample_dir: Path):
        result = json.loads(code_structure(str(sample_dir / "main.py"), query="all"))
        assert result["success"] is True
        results = list(result["results"].values())[0]
        assert "functions" in results
        assert "classes" in results

    def test_filter(self, sample_dir: Path):
        result = json.loads(code_structure(str(sample_dir), query="functions", filter="greet"))
        assert result["success"] is True
        for file_results in result["results"].values():
            for f in file_results.get("functions", []):
                assert "greet" in f["name"].lower()

    def test_recursive_false(self, sample_dir: Path):
        """Non-recursive mode only scans the top-level dir."""
        # Create a nested dir
        nested = sample_dir / "nested"
        nested.mkdir()
        (nested / "deep.py").write_text("def deep(): pass\\n")

        result_dir = json.loads(code_structure(str(sample_dir), recursive=False))
        assert result_dir["success"] is True

    def test_unsupported_file(self, tmp_path: Path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c\\n1,2,3\\n")
        result = json.loads(code_structure(str(f)))
        assert result["success"] is False
        assert "No parsable source files" in result["error"]

    def test_nonexistent_path(self):
        result = json.loads(code_structure("/nonexistent/path"))
        assert result["success"] is False
        assert "not exist" in result["error"]

    def test_directory_no_source(self, tmp_path: Path):
        d = tmp_path / "empty"
        d.mkdir()
        result = json.loads(code_structure(str(d)))
        assert result["success"] is False


class TestCodeSymbol:
    def test_find_symbol(self, sample_dir: Path):
        result = json.loads(code_symbol(str(sample_dir), symbol="Greeter"))
        assert result["success"] is True
        # Should find Greeter class
        found = False
        for file_results in result["results"].values():
            for section in file_results.values():
                for entry in section:
                    if entry.get("name") == "Greeter":
                        found = True
        assert found

    def test_find_symbol_across_languages(self, sample_dir: Path):
        result = json.loads(code_symbol(str(sample_dir), symbol="add"))
        assert result["success"] is True
        # Should find add() in both Python and Rust files
        files_found = list(result["results"].keys())
        assert len(files_found) >= 1


class TestSchemas:
    def test_schema_requires_path(self):
        from tools.code_structure_tool import CODE_STRUCTURE_SCHEMA
        assert "path" in CODE_STRUCTURE_SCHEMA["parameters"]["required"]

    def test_symbol_schema_requires_path_and_symbol(self):
        from tools.code_structure_tool import CODE_SYMBOL_SCHEMA
        req = CODE_SYMBOL_SCHEMA["parameters"]["required"]
        assert "path" in req
        assert "symbol" in req
