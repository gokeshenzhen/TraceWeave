"""Narrow recoverable rejections before the SDK's unchanged input validator."""

from mcp import types

from config import SIGNAL_SEARCH_MAX_KEYWORDS
from src.schemas import KeywordLimitErrorResult


def recover_keyword_limit_errors(app):
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
