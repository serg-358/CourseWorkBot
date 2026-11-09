"""Выбор актуального снимка деталей и инструментов без перезаписи ИИ."""


def effective_staff_details(request) -> tuple[str, str]:
    """Учитывает пустые строки как явную очистку, старые правки — как наследование."""
    for edit in reversed(getattr(request, "staff_edits", None) or []):
        details = getattr(edit, "details", None)
        if details is not None:
            return details.parts, details.tools
    assessment = getattr(request, "ai_assessment", None)
    return (getattr(assessment, "parts", ""), getattr(assessment, "tools", ""))
