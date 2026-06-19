from __future__ import annotations
import json
from .base import AutoFixPipeline, ValidationError


class I18nValidator(AutoFixPipeline):
    """Validates JSON/locale files used by i18n engine."""

    def validate(self, file_path: str, content: str) -> list[ValidationError]:
        errors: list[ValidationError] = []

        if file_path.endswith(".json"):
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                errors.append(ValidationError(
                    file=file_path,
                    line=e.lineno,
                    message=f"JSON parse error: {e.msg}",
                ))
        elif file_path.endswith((".ini", ".properties")):
            for i, line in enumerate(content.splitlines(), 1):
                line = line.strip()
                if not line or line.startswith(("#", "[")):
                    continue
                if "=" not in line and ":" not in line:
                    errors.append(ValidationError(
                        file=file_path,
                        line=i,
                        message=f"Suspect line (no key=value): {line[:60]}",
                        severity="warning",
                    ))

        return errors
