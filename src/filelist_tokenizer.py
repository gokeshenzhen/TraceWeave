"""Shared tokenization for VCS/Xcelium filelists and command files."""

from __future__ import annotations

import shlex


def _strip_c_style_comments(text: str) -> str:
    """Strip C-style comments while preserving quoted token content.

    EDA filelists commonly accept ``//`` and ``/* ... */`` comments, which
    :mod:`shlex` does not recognize.  Comment markers inside single- or
    double-quoted tokens remain literal, as do escaped markers outside quotes.
    Newlines inside block comments are retained so surrounding tokens cannot
    be joined accidentally.
    """

    output: list[str] = []
    quote: str | None = None
    in_block_comment = False
    index = 0

    while index < len(text):
        char = text[index]

        if in_block_comment:
            if text.startswith("*/", index):
                in_block_comment = False
                index += 2
            else:
                if char in "\r\n":
                    output.append(char)
                index += 1
            continue

        if quote is not None:
            output.append(char)
            if quote == '"' and char == "\\" and index + 1 < len(text):
                index += 1
                output.append(text[index])
            elif char == quote:
                quote = None
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            output.append(char)
            index += 1
            continue

        if char == "\\" and index + 1 < len(text):
            # Preserve shlex escaping and do not interpret the escaped byte as
            # the beginning of a quote or comment.
            output.extend((char, text[index + 1]))
            index += 2
            continue

        if text.startswith("//", index):
            output.append(" ")
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue

        if text.startswith("/*", index):
            output.append(" ")
            in_block_comment = True
            index += 2
            continue

        output.append(char)
        index += 1

    if in_block_comment:
        raise ValueError("unterminated block comment")

    return "".join(output)


def tokenize_filelist(text: str) -> list[str]:
    """Tokenize one filelist without evaluating shell syntax.

    Backslash-newline pairs are filelist line continuations and are spliced
    before comment recognition.  ``shlex`` then handles quoting, escaping, and
    ``#`` comments after the C-style comments have been removed safely.
    """

    logical_text = text.replace("\\\r\n", "").replace("\\\n", "")
    return shlex.split(
        _strip_c_style_comments(logical_text),
        comments=True,
        posix=True,
    )
