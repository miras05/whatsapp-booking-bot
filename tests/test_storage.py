"""Очередь входящих, сессии и передача диалога человеку."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import storage


def test_duplicate_delivery_is_ignored():
    """Meta повторяет вебхук — клиент не должен попасть в очередь дважды."""
    assert storage.enqueue_message("wamid.1", "7701", "Аня", "привет") is True
    assert storage.enqueue_message("wamid.1", "7701", "Аня", "привет") is False
    assert len(storage.next_pending()) == 1


def test_processed_message_leaves_queue():
    storage.enqueue_message("wamid.2", "7701", "", "текст")
    storage.mark_processed("wamid.2")
    assert storage.next_pending() == []


def test_failed_message_waits_before_retry():
    storage.enqueue_message("wamid.3", "7701", "", "текст")
    storage.mark_failed("wamid.3", "timeout", max_attempts=5)
    # После неудачи сообщение не подхватывается сразу — сначала пауза.
    assert storage.next_pending() == []


def test_message_gives_up_after_max_attempts():
    storage.enqueue_message("wamid.4", "7701", "", "текст")
    for _ in range(3):
        storage.mark_failed("wamid.4", "boom", max_attempts=3)
    with storage._connect() as conn:
        row = conn.execute("SELECT * FROM inbox WHERE message_id='wamid.4'").fetchone()
    assert row["attempts"] == 3
    assert row["next_attempt_at"] is None
    assert storage.next_pending(max_attempts=3) == []


def test_session_roundtrip():
    storage.save_session("7701", "ENTERING_NAME", {"service_name": "Пилинг"})
    assert storage.get_session("7701") == ("ENTERING_NAME", {"service_name": "Пилинг"})


def test_stale_session_starts_over():
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO sessions (wa_id, state, data, updated_at) VALUES (?, ?, ?, ?)",
            ("7702", "CONFIRMING", "{}", old),
        )
    assert storage.get_session("7702") == ("START", {})


def test_handoff_expires():
    storage.set_human_active("7703", hours=1)
    assert storage.is_human_active("7703")

    storage.set_human_active("7704", hours=-1)
    assert not storage.is_human_active("7704")
    assert storage.purge_stale_handoffs() == 1
