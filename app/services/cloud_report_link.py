"""Build the verified Cloud report deep-link for an issued invoice.

The Cloud report URL is server-side configuration: it carries the account-book
path (a secret) baked into its value and exposes exactly one ``{doc_key}``
placeholder. This module fills that placeholder with the server-confirmed
AutoCount ``docKey`` and returns a redirect target.

It deliberately returns no AutoCount response data, no credentials, and no
account-book identity beyond what the verified template already contains. The
template itself is never sent to the client and never appears in browser assets,
the OpenAPI schema, or the committed source.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

#: Placeholder in the verified template that receives the AutoCount docKey.
_DOC_KEY_PLACEHOLDER = "{doc_key}"

#: Substrings that would mean a credential or secret leaked into the template
#: itself. The account-book path belongs in the template value (kept server-side)
#: but must never appear as a fillable placeholder, and no API key / secret may
#: be templated either.
_FORBIDDEN_SUBSTRINGS = (
    "{account_book_id}",
    "{account_book}",
    "{api_key}",
    "{apikey}",
    "{key_id}",
    "{keyid}",
    "{secret}",
    "{token}",
    "{credential}",
    "api_key=",
    "apikey=",
    "key_id=",
    "keyid=",
    "secret=",
    "token=",
    "credential=",
)


def build_cloud_report_url(template: str, doc_key: str) -> str:
    """Substitute the server-confirmed AutoCount ``docKey`` into the template.

    Raises ``ValueError`` when:

    - ``doc_key`` is blank (never redirect to an undefined invoice);
    - the template is not HTTPS (the live redirect requires a secure origin);
    - the template embeds a credential or account-book placeholder.

    Only the ``{doc_key}`` placeholder is substituted, URL-encoded, and no
    AutoCount response data or credential is attached to the result.
    """
    if not isinstance(doc_key, str) or not doc_key.strip():
        raise ValueError("doc_key must be a non-blank invoice identifier")

    if not isinstance(template, str) or not template.strip():
        raise ValueError("cloud report template is not configured")

    template = template.strip()

    lowered = template.lower()
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        if forbidden in lowered:
            raise ValueError(
                f"cloud report template must not contain credential or "
                f"account-book placeholders: {forbidden!r}"
            )

    parsed = urlsplit(template)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("cloud report template must use https")

    placeholder_count = template.count(_DOC_KEY_PLACEHOLDER)
    if placeholder_count != 1:
        raise ValueError(
            "cloud report template must contain exactly one {doc_key} placeholder"
        )

    encoded = quote(doc_key.strip(), safe="")
    return template.replace(_DOC_KEY_PLACEHOLDER, encoded)
