from moseq2_test.provenance import redact_url, redacted_environment


def test_url_queries_are_removed() -> None:
    assert (
        redact_url("https://example.test/object?signature=secret#fragment")
        == "https://example.test/object"
    )


def test_sensitive_environment_values_are_redacted() -> None:
    value = redacted_environment(
        {"API_TOKEN": "secret", "DOWNLOAD_URL": "https://example.test/a?sig=x", "PLAIN": "ok"}
    )
    assert value == {
        "API_TOKEN": "<redacted>",
        "DOWNLOAD_URL": "https://example.test/a",
        "PLAIN": "ok",
    }
