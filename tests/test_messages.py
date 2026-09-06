"""The two message tools.

They stand on very different ground. `tests/fixtures/messages_of_day.json` is a real
`newsWidgetData` response from the school that publishes announcements, so that mapping is
verified — including the HTML the school writes them in. The inbox, by contrast, was empty
on the one tenant that grants access to it, so only its envelope is known and its item shape
is guessed; those tests pin that a guess cannot produce a wrong message.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.fake_untis import TENANT_WITHOUT_MESSAGES, VIEWS_WITHOUT_MESSAGES, FakeUntis
from untis_mcp.config import Config
from untis_mcp.server import create_server
from untis_mcp.untis.client import MESSAGES, NEWS_WIDGET, ClientRegistry, UntisClient
from untis_mcp.untis.mapping import parse_messages, parse_messages_of_day, plain_text

FIXTURE = Path(__file__).parent / "fixtures" / "messages_of_day.json"
DAY = dt.date(2026, 8, 24)

CONFIG = Config.model_validate(
    {
        "profiles": [
            {
                "name": "anna",
                "server": "https://ajax.webuntis.com",
                "school": "gym-musterstadt",
                "username": "anna.mustermann",
                "password": "anna-plaintext",
            }
        ]
    }
)


def build(fake: FakeUntis | None = None):
    fake = fake or FakeUntis().serve_fixture(NEWS_WIDGET, "messages_of_day.json")
    fake.responses.setdefault(MESSAGES, {"incomingMessages": [], "readConfirmationMessages": []})
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(CONFIG.get("anna"), http=fake.http_client())
    return create_server(CONFIG, registry=registry), fake


# ----------------------------------------------------------------------- messages of day


@pytest.fixture(scope="module")
def real():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_messages_of_day(payload, "anna", DAY)


def test_a_real_announcement_comes_through(real):
    assert real.profile == "anna"
    assert real.date == DAY
    assert len(real.messages) == 2
    assert "Fotograf" in real.messages[0].text


def test_the_schools_html_is_turned_into_readable_text(real):
    """The school writes these in a rich-text box; `<br>` read aloud helps nobody."""
    photographer = real.messages[0].text

    assert "<br>" not in photographer
    assert "\n" in photographer  # ... the break became a line break
    assert photographer.splitlines()[-1].startswith("Die Sch")


def test_an_empty_subject_stays_empty_rather_than_being_invented(real):
    """These notices routinely carry only text, and the subject is an empty string."""
    assert all(message.subject is None for message in real.messages)
    assert all(message.text for message in real.messages)


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("Eins<br>Zwei", "Eins\nZwei"),
        ("Eins<BR/>Zwei", "Eins\nZwei"),
        ("Eins<br />Zwei", "Eins\nZwei"),
        ("<p>Eins</p><p>Zwei</p>", "Eins\nZwei"),
        ("<ul><li>Eins</li><li>Zwei</li></ul>", "Eins\nZwei"),
        ("Gr&uuml;&szlig;e &amp; Dank", "Grüße & Dank"),
        ("<b>fett</b> und <i>kursiv</i>", "fett und kursiv"),
        ("   ", None),
        ("", None),
        (None, None),
        ("<br><br>", None),  # markup only, no words
    ],
)
def test_markup_conversion(markup, expected):
    assert plain_text(markup) == expected


def test_a_system_message_is_reported_separately():
    payload = {"data": {"systemMessage": "Wartung am Freitag", "messagesOfDay": []}}

    parsed = parse_messages_of_day(payload, "anna", DAY)

    assert parsed.system_message == "Wartung am Freitag"
    assert parsed.messages == []


def test_an_attachment_is_flagged_without_guessing_its_shape():
    payload = {
        "data": {
            "messagesOfDay": [
                {"subject": "Elternbrief", "text": "siehe Anhang", "attachments": [{"id": 1}]},
                {"subject": "", "text": "ohne Anhang", "attachments": []},
            ]
        }
    }

    parsed = parse_messages_of_day(payload, "anna", DAY)

    assert [item.has_attachments for item in parsed.messages] == [True, False]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {"messagesOfDay": None}},
        {"data": {"messagesOfDay": ["nonsense"]}},
        {"data": {"messagesOfDay": [{"subject": "", "text": ""}]}},
        {"data": {"messagesOfDay": [{}]}},
    ],
)
def test_a_malformed_announcement_payload_is_survivable(payload):
    parsed = parse_messages_of_day(payload, "anna", DAY)

    assert parsed.profile == "anna"
    assert parsed.messages == []


# --------------------------------------------------------------------------- the inbox


@pytest.mark.parametrize(
    "entry",
    [
        {"subject": "Elternabend", "content": "Am Dienstag"},
        {"title": "Elternabend", "body": "Am Dienstag"},
        {"name": "Elternabend", "text": "Am Dienstag"},
    ],
)
def test_plausible_inbox_shapes_are_read(entry):
    """The inbox was empty on the only tenant that grants it, so namings are tolerated."""
    parsed = parse_messages({"incomingMessages": [entry]}, "anna")

    assert parsed.messages[0].subject == "Elternabend"
    assert parsed.messages[0].body == "Am Dienstag"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"incomingMessages": None},
        {"incomingMessages": ["nonsense"]},
        {"incomingMessages": [{"unexpected": "shape"}]},
        {"incomingMessages": [{"subject": "", "content": ""}]},
    ],
)
def test_an_unreadable_message_is_skipped_not_reported_blank(payload):
    """A blank message in a parent's inbox would be worse than an absent one."""
    assert parse_messages(payload, "anna").messages == []


def test_a_message_awaiting_confirmation_is_marked():
    parsed = parse_messages(
        {
            "incomingMessages": [{"subject": "Info", "content": "x"}],
            "readConfirmationMessages": [{"subject": "Einverständnis", "content": "y"}],
        },
        "anna",
    )

    by_subject = {item.subject: item for item in parsed.messages}
    assert by_subject["Einverständnis"].needs_confirmation is True
    assert by_subject["Info"].needs_confirmation is False


def test_unread_messages_are_counted_and_sorted_newest_first():
    parsed = parse_messages(
        {
            "incomingMessages": [
                {
                    "subject": "Alt",
                    "content": "x",
                    "sentDateTime": "2026-09-01T08:00",
                    "isRead": True,
                },
                {
                    "subject": "Neu",
                    "content": "y",
                    "sentDateTime": "2026-09-05T08:00",
                    "isRead": False,
                },
            ]
        },
        "anna",
    )

    assert [item.subject for item in parsed.messages] == ["Neu", "Alt"]
    assert parsed.unread_count == 1


def test_a_sender_may_be_a_name_or_an_object():
    parsed = parse_messages(
        {
            "incomingMessages": [
                {"subject": "a", "content": "x", "sender": "Frau Ahlers"},
                {"subject": "b", "content": "y", "sender": {"displayName": "Herr Bode"}},
            ]
        },
        "anna",
    )

    assert {item.sender for item in parsed.messages} == {"Frau Ahlers", "Herr Bode"}


def test_epoch_milliseconds_are_accepted_as_a_sent_time():
    """Other endpoints date things in epoch millis, so this one might too."""
    parsed = parse_messages(
        {"incomingMessages": [{"subject": "a", "content": "x", "date": 1788415297875}]}, "anna"
    )

    assert parsed.messages[0].sent is not None
    assert parsed.messages[0].sent.year == 2026


# ------------------------------------------------------------------------------ the tools


async def test_the_announcement_tool_asks_for_the_right_date():
    server, fake = build()

    async with Client(server) as client:
        result = await client.call_tool(
            "get_messages_of_day", {"profile": "anna", "date": "2026-08-24"}
        )

    assert fake.data_params[-1]["date"] == "20260824"  # compact, per section 5.3
    assert len(result.data.messages) == 2
    assert result.data.profile == "anna"


async def test_the_announcement_tool_defaults_to_today():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_messages_of_day", {"profile": "anna"})

    assert fake.data_params[-1]["date"] == dt.date.today().strftime("%Y%m%d")


async def test_announcements_work_on_a_school_that_forbids_the_inbox():
    """The whole reason these are two tools rather than one with a fallback (ADR-010)."""
    fake = FakeUntis(
        permissions=list(TENANT_WITHOUT_MESSAGES), views=list(VIEWS_WITHOUT_MESSAGES)
    ).serve_fixture(NEWS_WIDGET, "messages_of_day.json")
    server, _ = build(fake)

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_messages", {"profile": "anna"})
        announcements = await client.call_tool("get_messages_of_day", {"profile": "anna"})

    assert "get_messages is not available" in str(excinfo.value)
    assert len(announcements.data.messages) == 2


async def test_the_inbox_tool_returns_messages_and_respects_its_filters():
    fake = FakeUntis().serve_fixture(NEWS_WIDGET, "messages_of_day.json")
    fake.responses[MESSAGES] = {
        "incomingMessages": [
            {"subject": "Gelesen", "content": "x", "isRead": True},
            {"subject": "Ungelesen", "content": "y", "isRead": False},
        ],
        "readConfirmationMessages": [],
    }
    server, _ = build(fake)

    async with Client(server) as client:
        every = await client.call_tool("get_messages", {"profile": "anna"})
        unread = await client.call_tool("get_messages", {"profile": "anna", "unread_only": True})
        capped = await client.call_tool("get_messages", {"profile": "anna", "limit": 1})

    assert len(every.data.messages) == 2
    assert [item.subject for item in unread.data.messages] == ["Ungelesen"]
    assert len(capped.data.messages) == 1
    # The count keeps describing what was found, not what survived the filter.
    assert unread.data.unread_count == 1


async def test_an_empty_inbox_is_an_answer_not_a_failure():
    server, _ = build()

    async with Client(server) as client:
        result = await client.call_tool("get_messages", {"profile": "anna"})

    assert result.data.messages == []
    assert result.data.unread_count == 0


async def test_both_tools_are_advertised_with_only_profile_required():
    server, _ = build()

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools["get_messages"].input_schema["required"] == ["profile"]
    assert tools["get_messages_of_day"].input_schema["required"] == ["profile"]
    # The inbox description must point at the other tool, since one school forbids it.
    assert "get_messages_of_day" in (tools["get_messages"].description or "")
