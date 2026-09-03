"""Safe arithmetic + Korean amount formatting for the agent.

The model must not do arithmetic or unit conversion in its head: it read
"227,500,000,000원" correctly from a filing and then reported it as
"2조 275억원" (the right value is 2,275억원). Numbers are the single most
heavily weighted evaluation criterion, so every computation goes through
code instead.

`evaluate()` runs on a whitelisted AST — no eval(), no names, no calls — so
a hostile or malformed expression can only fail, never execute anything.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

_MAX_EXPR_CHARS = 500
_MAX_ABS = 1e30  # 결과가 이 범위를 넘으면 오류 (오타·폭주 방지)
_MAX_POW_EXP = 64

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalcError(Exception):
    """The expression could not be evaluated (bad syntax, unsupported node…)."""


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError(f"숫자가 아닌 값은 쓸 수 없습니다: {node.value!r}")
        return float(node.value)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise CalcError("지원하지 않는 단항 연산자입니다")
        return op(_eval_node(node.operand))

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalcError("지원하지 않는 연산자입니다 (+, -, *, /, //, %, ** 만 가능)")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXP:
            raise CalcError(f"지수가 너무 큽니다 (최대 {_MAX_POW_EXP})")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise CalcError("0으로 나눌 수 없습니다")
        try:
            return op(left, right)
        except (OverflowError, ValueError) as e:
            raise CalcError(f"계산할 수 없습니다: {e}") from e

    raise CalcError(f"허용되지 않은 표현입니다: {type(node).__name__}")


def evaluate(expression: str) -> float:
    """Evaluates an arithmetic expression. Commas in numbers are allowed
    because filing amounts are written that way (227,500,000,000)."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("수식이 비어 있습니다")
    expr = expression.strip()
    if len(expr) > _MAX_EXPR_CHARS:
        raise CalcError(f"수식이 너무 깁니다 (최대 {_MAX_EXPR_CHARS}자)")

    # 공시 금액 표기(227,500,000,000)를 그대로 받기 위해 자리구분 쉼표를 제거.
    # 함수 호출이 없는 문법이므로 쉼표는 튜플 외에는 의미가 없다.
    cleaned = expr.replace(",", "").replace("_", "")
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        raise CalcError(f"수식 문법이 잘못되었습니다 — 항목 이름이 아니라 실제 숫자만 넣으세요 (예: 29.2-7.7): {e.msg}") from e

    result = _eval_node(tree)
    if math.isnan(result) or math.isinf(result):
        raise CalcError("계산 결과가 유효한 숫자가 아닙니다")
    if abs(result) > _MAX_ABS:
        raise CalcError("계산 결과가 너무 큽니다")
    return result


_UNITS = ((10**12, "조"), (10**8, "억"), (10**4, "만"))


def korean_amount(value: float) -> str:
    """Renders a won amount in Korean units.

    227_500_000_000    -> "2,275억"
    2_027_500_000_000  -> "2조 275억"
    4_004_923_296_000  -> "4조 49억 2,329만 6,000"
    """
    if value != value or value in (float("inf"), float("-inf")):
        return ""
    sign = "-" if value < 0 else ""
    n = abs(value)

    # 소수는 단위 분해가 의미 없으므로 그대로 표기
    if n != int(n):
        return f"{sign}{n:,.4g}"
    n = int(n)
    if n == 0:
        return "0"

    parts: list[str] = []
    for unit_value, unit_name in _UNITS:
        if n >= unit_value:
            q, n = divmod(n, unit_value)
            parts.append(f"{q:,}{unit_name}")
    if n:
        parts.append(f"{n:,}")
    return sign + " ".join(parts)


def korean_amount_short(value: float) -> str:
    """읽기용 근사 표기 — 상위 2단위까지만 남긴다.

    227_500_000_000   -> "2,275억"
    2_020_287_132_255 -> "약 2조 202억"
    재무제표 한 섹션에 금액이 180개씩 나오므로 전체 표기를 붙이면 본문이
    두 배가 된다. 정확한 값은 원문 숫자가 이미 갖고 있으니, 옆에 붙이는
    환산은 사람이 말하는 방식(상위 2단위)이면 충분하다."""
    full = korean_amount(value)
    if not full or full.startswith("-") and len(full) < 2:
        return full
    sign = "-" if full.startswith("-") else ""
    parts = full.lstrip("-").split(" ")
    if len(parts) <= 2:
        return sign + " ".join(parts)
    return f"{sign}약 " + " ".join(parts[:2])


def calculate(expression: str) -> dict[str, Any]:
    """Tool-facing wrapper: value + both renderings, so the model never has
    to reformat a number itself."""
    result = evaluate(expression)
    rounded = round(result, 6)
    if rounded == int(rounded):
        rounded = int(rounded)
    return {
        "expression": expression,
        "result": rounded,
        "formatted": f"{rounded:,}",
        "korean_amount": korean_amount(result),
    }
