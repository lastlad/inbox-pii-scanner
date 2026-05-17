"""Tests for ``_build_query`` in inbox_scanner.gmail.sync.

The query string is what we hand to Gmail's ``messages.list?q=...``.
Wrong tokens here silently change which mail we sync — worth pinning.
"""

from __future__ import annotations

from inbox_scanner.gmail.sync import MailboxScope, _build_query


def test_default_query_is_bare_has_attachment():
    assert _build_query(None) == "has:attachment"


def test_inbox_scope_adds_in_inbox():
    assert _build_query(None, MailboxScope.INBOX) == "has:attachment in:inbox"


def test_sent_scope_adds_in_sent():
    assert _build_query(None, MailboxScope.SENT) == "has:attachment in:sent"


def test_all_scope_adds_no_label_filter():
    # 'all' is the documented default — bare ``has:attachment`` matches
    # every label except spam/trash. Asserting equality with the default
    # protects against accidentally adding noise like ``in:anywhere``.
    assert _build_query(None, MailboxScope.ALL) == "has:attachment"


def test_since_appended_with_yyyy_slash_format():
    # ISO YYYY-MM-DD → Gmail's YYYY/MM/DD.
    assert _build_query("2026-01-15") == "has:attachment after:2026/01/15"


def test_since_combines_with_mailbox_scope():
    assert (
        _build_query("2026-01-15", MailboxScope.SENT)
        == "has:attachment in:sent after:2026/01/15"
    )


def test_mailbox_scope_string_values_are_stable():
    # The Enum values are persisted in syncs.mailbox_scope; changing the
    # strings would invalidate existing rows.
    assert MailboxScope.ALL.value == "all"
    assert MailboxScope.INBOX.value == "inbox"
    assert MailboxScope.SENT.value == "sent"
