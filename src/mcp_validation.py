"""Recoverable input errors and bounded reuse of checked JSON schemas."""

from collections import OrderedDict
from itertools import islice
import json

import jsonschema
from mcp import types
from pydantic import ValidationError

from config import SIGNAL_SEARCH_MAX_KEYWORDS
from src.schemas import CycleInputErrorResult, KeywordLimitErrorResult, WaveformExpression, ExpressionToolErrorResult
from src.expression_errors import ExpressionError, input_validation_issues


class ToolInputValidators:
    """Cache schema validators, never request arguments or validation results.

    The SDK's jsonschema.validate checks the entire schema on every call.
    Check each exact schema once and retain its private decoded copy. Encoding
    on lookup also detects in-place changes to a published tool definition.
    Bounds apply to entry count and retained serialized schema bytes.
    """

    def __init__(self, *, max_entries=128, max_bytes=256 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries = OrderedDict()
        self.bytes = 0

    def validate(self, name, schema, arguments):
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        previous = self.entries.get(name)
        if previous is not None and previous[0] == encoded:
            self.entries.move_to_end(name)
            validator = previous[1]
        else:
            snapshot = json.loads(encoded)
            cls = jsonschema.validators.validator_for(snapshot)
            cls.check_schema(snapshot)
            validator = cls(snapshot)
            size = len(encoded.encode("utf-8"))
            if previous is not None:
                self.bytes -= self.entries.pop(name)[2]
            if self.max_entries > 0 and size <= self.max_bytes:
                while self.entries and (len(self.entries) >= self.max_entries
                                        or self.bytes + size > self.max_bytes):
                    self.bytes -= self.entries.popitem(last=False)[1][2]
                self.entries[name] = (encoded, validator, size)
                self.bytes += size
        # Match jsonschema.validate's error selection, including anyOf/oneOf.
        error = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
        if error is not None:
            raise error


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

    Register the SDK handler with validate_input=False: this wrapper performs
    the same jsonschema instance validation using checked, bounded validators.
    SDK exception handling and output normalization remain in place. Rejected
    inputs do not enter tool-handler telemetry.
    """

    def decorate(func):
        sdk_handler = app.request_handlers[types.CallToolRequest]
        validators = ToolInputValidators()

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
            try:
                tool = await app._get_cached_tool_definition(request.params.name)
                if tool is not None:
                    validators.validate(tool.name, tool.inputSchema, arguments)
            except jsonschema.ValidationError as exc:
                message = f"Input validation error: {exc.message}"
                if (request.params.name == "parse_sim_log"
                        and "sim_log" in arguments and "log_path" not in arguments):
                    message += " Use log_path for parse_sim_log; sim_log is a get_sim_paths input."
                return app._make_error_result(message)
            except Exception as exc:
                return app._make_error_result(str(exc))
            return await sdk_handler(request)

        app.request_handlers[types.CallToolRequest] = handler
        return func

    return decorate
