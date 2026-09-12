"""Normalization and ordinal ranking; never infer an athlete's outdoor ability."""

import re


def normalize_discipline(value: str | None, wall_style: str | None = None) -> str | None:
    text = (value or "").lower().replace(" ", "_")
    wall = (wall_style or "").lower()
    if "auto" in text or "auto" in wall or text in {"автостраховка", "автоспуск"}:
        return "auto_belay"
    if "boulder" in text or "болдер" in text:
        return "bouldering"
    if text in {"bottom_rope", "lead", "нижняя_страховка"}:
        return "lead"
    if text in {"top_rope", "toprope"}:
        return "top_rope"
    if text in {"multipitch", "outdoor"}:
        return text
    if text in {"rope", "route", "routes", "rope_climbing", "roped_climbing"}:
        return "roped_climbing"
    return text or None


def normalize_grade(system: str | None, value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return system, None
    grade = value.strip().translate(str.maketrans("абсАБС", "abcABC"))
    grade = grade.replace("–", "-").replace("—", "-")
    if re.fullmatch(r"[vV]\d+(?:-?[vV]?\d+)?", grade):
        return "V", grade.upper()
    if re.fullmatch(r"5\.\d+[a-dA-D]?[+-]?", grade):
        return "YDS", grade.lower()
    if re.fullmatch(r"[3-9][a-cA-C][+]?", grade) or (
        (system or "").lower() in {"french", "font", "fontainebleau"}
        and re.fullmatch(r"[1-9][+-]?", grade)
    ):
        font = (system or "").lower() in {"font", "fontainebleau"}
        return ("Font" if font else "French"), (grade.upper() if font else grade.lower())
    # "easy routes" is a description, never an exact grade.
    return system, None


def grade_rank(system: str | None, value: str | None) -> float | None:
    system, grade = normalize_grade(system, value)
    if not grade:
        return None
    if system == "V":
        return float(grade[1:]) if re.fullmatch(r"V\d+", grade) else None
    if system in {"French", "Font"}:
        match = re.fullmatch(r"([1-9])([abc])?(\+)?", grade.lower())
        # A broad "5s" range is not a precise highest grade.
        if not match or not match[2]:
            return None
        return int(match[1]) * 6 + {"a": 0, "b": 2, "c": 4}[match[2]] + bool(match[3])
    if system == "YDS":
        match = re.fullmatch(r"5\.(\d+)([abcd])?([+-])?", grade)
        if match:
            return (
                int(match[1])
                + {None: 0, "a": 0.1, "b": 0.3, "c": 0.6, "d": 0.8}[match[2]]
                + {None: 0, "+": 0.05, "-": -0.05}[match[3]]
            )
    return None
