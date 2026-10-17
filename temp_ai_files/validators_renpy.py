from __future__ import annotations
import ast
import re
import subprocess
from pathlib import Path
from .base import AutoFixPipeline, ValidationError


class RenpyValidator(AutoFixPipeline):
    """Validates Ren'Py .rpy files: ast.parse on Python blocks + $ lines + renpy lint."""

    _PYTHON_BLOCK_RE = re.compile(
        r"^(?:init\s+)?(?:\d+\s+)?python\s*(?:early|late)?:",
        re.MULTILINE,
    )

    _KNOWN_BASES = frozenset({
        "object", "dict", "list", "tuple", "set", "frozenset",
        "str", "int", "float", "bool", "bytes", "type",
        "Exception", "BaseException", "ValueError", "TypeError",
        "RuntimeError", "StopIteration", "NotImplementedError",
        "OverflowError", "ZeroDivisionError", "KeyError", "IndexError",
        "AttributeError", "NameError", "SyntaxError", "IOError",
        "OSError", "FileNotFoundError", "PermissionError",
        "displayable", "Displayable", "Imageable",
        "Solid", "Frame", "Window", "VBox", "HBox", "Fixed",
        "Grid", "Side", "HasTransform", "Fixed", "DynamicDisplayable",
        "ConditionSwitch", "ShowingSwitch", "Text", "Input",
        "Button", "Imagebutton", "Hotbar", "Bar", "VBar", "HBar",
        "Scrollbar", "VScrollbar", "HScrollbar", "Slider",
        "Viewport", "Drag", "DragGroup", "Cursors",
        "Timer", "At", "Transform", "Controller",
        "Layout", "MultiBox", "MultiGround", "Animation",
        "Position", "ZoomTransform", "RotoZoom", "FactorZoom",
        "CropTransform", "OldTransform", "AlphaDissolve",
        "AlphaBlend", "Dissolve", "ImageDissolve", "CropTransition",
        "Fade", "MoveTransition", "MoveinTransition", "MoveoutTransition",
        "PauseTransition", "PowerDissolve", "PushMove", "SlideTransition",
        "Slider", "hpunch", "vpunch", "pixellate",
    })

    def validate(self, file_path: str, content: str) -> list[ValidationError]:
        errors: list[ValidationError] = []

        for m in self._PYTHON_BLOCK_RE.finditer(content):
            start = content[:m.end()].count("\n") + 1
            block_start = m.end()
            rest = content[block_start:]
            first_nonempty = ""
            for bl in rest.split("\n")[1:]:
                if bl.strip():
                    first_nonempty = bl
                    break
            indent = len(first_nonempty) - len(first_nonempty.lstrip()) if first_nonempty else 0

            lines = rest.split("\n")
            block_lines = []
            for line in lines[1:]:
                stripped = line.rstrip()
                if stripped == "":
                    block_lines.append("")
                    continue
                line_indent = len(line) - len(line.lstrip())
                if line_indent < indent:
                    break
                block_lines.append(stripped[indent:])

            if block_lines:
                code = "\n".join(block_lines)
                try:
                    tree = ast.parse(code)
                except SyntaxError as e:
                    errors.append(ValidationError(
                        file=file_path,
                        line=start + (e.lineno or 1),
                        message=f"Python syntax error in init python block: {e.msg}",
                    ))
                else:
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ClassDef):
                            for base in node.bases:
                                name = ""
                                if isinstance(base, ast.Name):
                                    name = base.id
                                elif isinstance(base, ast.Attribute):
                                    name = base.attr
                                if name and name not in self._KNOWN_BASES:
                                    errors.append(ValidationError(
                                        file=file_path,
                                        line=start + (node.lineno or 1),
                                        message=f"Undefined base class '{name}' in class '{node.name}'",
                                    ))

        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("$ "):
                code = stripped[2:]
                try:
                    ast.parse(code)
                except SyntaxError as e:
                    errors.append(ValidationError(
                        file=file_path,
                        line=i,
                        message=f"Python syntax error in $ statement: {e.msg}",
                    ))

        try:
            result = subprocess.run(
                ["renpy", file_path, "lint"],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(file_path).parent),
            )
            if result.returncode != 0:
                for line in result.stdout.splitlines() + result.stderr.splitlines():
                    if "error" in line.lower() or "exception" in line.lower():
                        errors.append(ValidationError(
                            file=file_path,
                            message=f"renpy lint: {line.strip()}",
                        ))
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            pass

        return errors
