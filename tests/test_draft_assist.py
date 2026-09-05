import pytest

from savvy_scout.models.notice import Notice
from savvy_scout.sources.ocds_parser import ParsedNotice
from savvy_scout.sweep.dedupe import upsert_notice
from savvy_scout.triage.draft_assist import (
    SYSTEM_INSTRUCTIONS,
    get_draft_assist_client,
    run_draft_answer,
    run_draft_answer_openai,
)


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, content_text, stop_reason="end_turn"):
        self.content = [FakeTextBlock(content_text)]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


class FakeOpenAIChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = type("M", (), {"content": content})()
        self.finish_reason = finish_reason


class FakeOpenAIResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeOpenAIChoice(content, finish_reason)]


class FakeOpenAICompletions:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeOpenAIClient:
    def __init__(self, response):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeOpenAICompletions(response)


def _make_notice(conn):
    notice = Notice(
        ref="REF-DRAFT-1",
        title="Data Platform Migration",
        buyer="Some Council",
        source="Find a Tender",
        notice_type="UK3",
        uk_stage="UK3",
        raw_json="{}",
        cpv_primary="72200000",
    )
    parsed = ParsedNotice(
        notice=notice,
        text_blob="migrate a legacy data platform to a modern cloud stack",
        tender_status="active",
    )
    return upsert_notice(conn, parsed)


def test_run_draft_answer_returns_model_text(conn):
    notice_id = _make_notice(conn)
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    client = FakeClient(FakeResponse("We have delivered three comparable data platform migrations..."))

    answer = run_draft_answer(client, conn, notice_row, "Describe your experience with data platform migrations.")

    assert answer == "We have delivered three comparable data platform migrations..."
    assert client.messages.last_kwargs["system"][0]["text"] == SYSTEM_INSTRUCTIONS
    assert "Describe your experience" in client.messages.last_kwargs["messages"][0]["content"]


def test_run_draft_answer_raises_on_refusal(conn):
    notice_id = _make_notice(conn)
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    client = FakeClient(FakeResponse("", stop_reason="refusal"))

    with pytest.raises(RuntimeError, match="declined to draft"):
        run_draft_answer(client, conn, notice_row, "A question")


def test_run_draft_answer_openai_returns_model_text(conn):
    notice_id = _make_notice(conn)
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    client = FakeOpenAIClient(FakeOpenAIResponse("A drafted answer"))

    answer = run_draft_answer_openai(client, conn, notice_row, "A question")

    assert answer == "A drafted answer"


def test_run_draft_answer_openai_raises_on_content_filter(conn):
    notice_id = _make_notice(conn)
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    client = FakeOpenAIClient(FakeOpenAIResponse(None, finish_reason="content_filter"))

    with pytest.raises(RuntimeError, match="declined to draft"):
        run_draft_answer_openai(client, conn, notice_row, "A question")


def test_get_draft_assist_client_raises_without_anthropic_key():
    settings = type("S", (), {"scope_read_provider": "anthropic", "anthropic_api_key": None, "openai_api_key": None})()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        get_draft_assist_client(settings)


def test_get_draft_assist_client_raises_without_openai_key():
    settings = type("S", (), {"scope_read_provider": "openai", "anthropic_api_key": None, "openai_api_key": None})()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_draft_assist_client(settings)


def test_get_draft_assist_client_returns_anthropic_client_when_configured():
    settings = type("S", (), {"scope_read_provider": "anthropic", "anthropic_api_key": "sk-test", "openai_api_key": None})()
    client, draft_fn, model_name = get_draft_assist_client(settings)
    assert draft_fn is run_draft_answer
    assert model_name == "claude-sonnet-5"
