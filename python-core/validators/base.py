from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ValidationError:
    file: str
    line: int | None = None
    message: str = ""
    severity: str = "error"


@dataclass
class FixResult:
    file: str
    original: str
    fixed: str
    error: str = ""


class AutoFixPipeline(ABC):
    """Base class for engine-specific validation + auto-fix.

    Subclasses implement validate() for their engine's syntax.
    The fix loop (LLM reads error → patches string) is shared.
    """

    @abstractmethod
    def validate(self, file_path: str, content: str) -> list[ValidationError]:
        ...

    def validate_file(self, file_path: str) -> list[ValidationError]:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return [ValidationError(file=file_path, message=f"Cannot read: {e}")]
        return self.validate(file_path, content)
