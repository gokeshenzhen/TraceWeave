from src.filelist_tokenizer import tokenize_filelist


def test_embedded_double_slash_is_token_content_not_a_line_comment():
    tokens = tokenize_filelist(
        "// disabled entry\n"
        "-f $LIB_ROOT//lists/design.f // trailing comment\n"
        "+define+DOC_URL=http://intranet/spec\n"
        "top.sv\n"
    )

    assert tokens == [
        "-f",
        "$LIB_ROOT//lists/design.f",
        "+define+DOC_URL=http://intranet/spec",
        "top.sv",
    ]
