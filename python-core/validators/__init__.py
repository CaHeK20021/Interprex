from .base import AutoFixPipeline, ValidationError
from .renpy import RenpyValidator
from .i18n import I18nValidator

VALIDATORS = {
    "renpy": RenpyValidator,
    "i18n": I18nValidator,
}

def get_validator(engine: str) -> AutoFixPipeline | None:
    cls = VALIDATORS.get(engine)
    return cls() if cls else None
