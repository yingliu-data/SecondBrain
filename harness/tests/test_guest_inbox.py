"""guest.inbox — classify control messages as input events (docs/GUEST.md)."""
import pytest

from guest.inbox import Inbox
from protocol.fold import State, fold
from protocol.messages import control_message
from tests.logbuilder import Log

SID = "sess-test"


def ctl(type, body, cseq=1):
    return control_message(SID, cseq, type, body)


def state_with_question(deadline=1_000, clock_at=None, answered=False) -> State:
    log = Log()
    log.start()
    log.input("in-1", "ask me")
    log.turn("turn-1")
    log.response("turn-1", calls=[("call-1", "question", {"text": "which?"})])
    log.call("turn-1", "call-1", "question", "guest", {"text": "which?"})
    log.add("question", {"question_id": "q-1", "call_id": "call-1", "text": "which?",
                         "options": ["a", "b"], "deadline": deadline})
    if clock_at is not None:
        log.clock(clock_at, kind="question_deadline")
    if answered:
        log.input("in-2", "a", kind="ui_response", question_id="q-1", answer="a")
    return fold(log.events)


def test_task_new():
    inbox = Inbox(State())
    dedupe, body = inbox.classify(ctl("task", {"input_id": "in-7", "text": "hi", "attachments": []}))
    assert dedupe == "new"
    assert body == {"input_id": "in-7", "kind": "task", "text": "hi", "dedupe": "new",
                    "attachments": []}


def test_attachments_carried_and_defaulted():
    inbox = Inbox(State())
    memories = [{"slug": "m1", "text": "remembered"}]
    _, body = inbox.classify(ctl("follow_up", {"input_id": "in-8", "text": "more",
                                               "attachments": memories}))
    assert body["kind"] == "follow_up"
    assert body["attachments"] == memories
    _, body = inbox.classify(ctl("steer", {"input_id": "in-9", "text": "stop"}))
    assert body["kind"] == "steer"
    assert body["attachments"] == []


def test_duplicate_by_input_id():
    log = Log()
    log.start()
    log.input("in-1", "hi")
    inbox = Inbox(fold(log.events))
    dedupe, body = inbox.classify(ctl("task", {"input_id": "in-1", "text": "hi again",
                                               "attachments": []}))
    assert dedupe == "duplicate"
    assert body["dedupe"] == "duplicate"
    assert body["text"] == "hi again"
    dedupe, _ = inbox.classify(ctl("task", {"input_id": "in-2", "text": "x", "attachments": []}))
    assert dedupe == "new"


def test_classify_does_not_mutate_state():
    state = State()
    inbox = Inbox(state)
    inbox.classify(ctl("task", {"input_id": "in-1", "text": "hi", "attachments": []}))
    inbox.classify(ctl("task", {"input_id": "in-1", "text": "hi", "attachments": []}))
    assert state.seen_input_ids == set()


def test_ui_response_open_question_is_new():
    inbox = Inbox(state_with_question(deadline=1_000, clock_at=500))
    dedupe, body = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                      "answer": "a"}))
    assert dedupe == "new"
    assert body == {"input_id": "in-5", "kind": "ui_response", "text": "a", "dedupe": "new",
                    "question_id": "q-1", "answer": "a"}


def test_ui_response_unknown_question_is_late():
    inbox = Inbox(state_with_question())
    dedupe, body = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-9",
                                                      "answer": "a"}))
    assert dedupe == "late"
    assert body["dedupe"] == "late"
    assert body["question_id"] == "q-9"


def test_ui_response_after_deadline_is_late():
    inbox = Inbox(state_with_question(deadline=1_000, clock_at=1_001))
    dedupe, _ = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                   "answer": "a"}))
    assert dedupe == "late"
    # the caller's own clock overrides the fold's
    inbox = Inbox(state_with_question(deadline=1_000, clock_at=500))
    dedupe, _ = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                   "answer": "a"}), at=2_000)
    assert dedupe == "late"
    dedupe, _ = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                   "answer": "a"}), at=1_000)
    assert dedupe == "new"


def test_ui_response_no_clock_yet_is_new():
    inbox = Inbox(state_with_question(deadline=1_000, clock_at=None))
    dedupe, _ = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                   "answer": "a"}))
    assert dedupe == "new"


def test_ui_response_answered_twice_is_late_then_duplicate():
    inbox = Inbox(state_with_question(answered=True))
    dedupe, _ = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                   "answer": "b"}))
    assert dedupe == "late"
    dedupe, _ = inbox.classify(ctl("ui_response", {"input_id": "in-2", "question_id": "q-1",
                                                   "answer": "a"}))
    assert dedupe == "duplicate"


def test_ui_response_non_string_answer_is_serialised():
    inbox = Inbox(state_with_question())
    _, body = inbox.classify(ctl("ui_response", {"input_id": "in-5", "question_id": "q-1",
                                                 "answer": {"choice": 2}}))
    assert body["text"] == '{"choice": 2}'
    assert body["answer"] == body["text"]


def test_malformed_messages_raise():
    inbox = Inbox(State())
    with pytest.raises(ValueError, match="not an input control message: 'heartbeat'"):
        inbox.classify(ctl("heartbeat", {"at": 1}))
    with pytest.raises(ValueError, match="task.input_id"):
        inbox.classify(ctl("task", {"text": "no id"}))
    with pytest.raises(ValueError, match="ui_response.question_id"):
        inbox.classify(ctl("ui_response", {"input_id": "in-1", "answer": "a"}))
    with pytest.raises(ValueError, match="task.attachments"):
        inbox.classify(ctl("task", {"input_id": "in-1", "text": "x", "attachments": "m"}))
    with pytest.raises(ValueError, match="task.text"):
        inbox.classify(ctl("task", {"input_id": "in-1", "text": 5}))
