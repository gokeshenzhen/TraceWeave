"""Narrow recoverable rejections before the SDK's unchanged input validator."""

from itertools import islice

from mcp import types
from pydantic import ValidationError

from config import SIGNAL_SEARCH_MAX_KEYWORDS
from src.schemas import CycleInputErrorResult, KeywordLimitErrorResult, WaveformExpression, ExpressionToolErrorResult
from src.expression_errors import ExpressionError, input_validation_issues


def expression_input_error(arguments):
    """Bounded field-only check; no parsing, type inference or waveform access."""
    pending = [("", arguments)]
    issues = []
    for _ in range(512):
        if not pending or len(issues) >= 16:
            break
        path, value = pending.pop()
        if isinstance(value, dict) and 'expr' in value:
            try:
                WaveformExpression.model_validate(value)
            except ValidationError as exc:
                issues.extend(input_validation_issues(exc, value, path))
        elif isinstance(value, (dict, list)):
            entries = value.items() if isinstance(value, dict) else enumerate(value)
            pending.extend((f'{path}.{key}' if path else str(key), child)
                           for key, child in reversed(list(islice(entries, 128))))
    if issues:
        error = ExpressionError('expression_input_invalid', parameter=issues[0]['parameter'],
                                message='Input validation error: invalid expression fields.', issues=issues)
        return ExpressionToolErrorResult.model_validate(error.payload())
    return None


def cycle_input_error(arguments):
    """Report all recognized cycle conflicts without changing sampling semantics."""
    issues = []
    if "start_time_ps" in arguments and "start_cycle" in arguments:
        issues.append("start_time_ps and start_cycle are mutually exclusive; omit one.")
    offset = arguments.get("sample_offset_ps", 1)
    if type(offset) is int and offset < 0:
        issues.append("sample_offset_ps must be >= 0; for pre-edge inputs use sample_phase=before and omit sample_offset_ps.")
    if arguments.get("sample_phase") == "before" and "sample_offset_ps" in arguments and type(offset) is int and offset != 0:
        issues.append("sample_phase=before requires sample_offset_ps=0 or omission.")
    if not issues:
        return None
    return CycleInputErrorResult(
        error="Input validation error: invalid cycle sampling arguments.",
        issues=issues,
        recovery=(
            "Fix every listed issue. Choose start_cycle OR start_time_ps. "
            "end_time_ps may be combined with num_cycles to limit returned window edges. "
            "For pre-edge inputs use sample_phase=before and omit sample_offset_ps; "
            "for post-edge values use sample_phase=after (default offset 1ps)."
        ),
    )


def recover_tool_input_errors(app):
    """Decorate an already registered call_tool handler without replacing it.

    This precheck only rejects requests; it never admits a request or invokes
    the tool. Every other request still goes through the SDK's original schema
    validation, exception handling and output normalization. Like SDK input
    failures, these rejections do not enter the tool-handler telemetry.
    """

    def decorate(func):
        sdk_handler = app.request_handlers[types.CallToolRequest]

        async def handler(request):
            arguments = request.params.arguments or {}
            error = (
                cycle_input_error(arguments)
                if request.params.name == "get_signals_by_cycle" else None
            )
            keyword = arguments.get("keyword")
            if (
                request.params.name == "search_signals"
                and isinstance(keyword, list)
                and len(keyword) > SIGNAL_SEARCH_MAX_KEYWORDS
            ):
                error = KeywordLimitErrorResult(
                    error="Input validation error: too many search keywords.",
                    provided_count=len(keyword),
                    max_count=SIGNAL_SEARCH_MAX_KEYWORDS,
                    recovery=(
                        "Split the request into batches of at most "
                        f"{SIGNAL_SEARCH_MAX_KEYWORDS} keywords; preserve input order."
                    ),
                )
            if error is None:
                error = expression_input_error(arguments)
            if error is not None:
                return types.ServerResult(types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(
                        type="text", text=error.model_dump_json(exclude_none=True)
                    )],
                    structuredContent=error.model_dump(exclude_none=True),
                ))
            return await sdk_handler(request)

        app.request_handlers[types.CallToolRequest] = handler
        return func

    return decorate
