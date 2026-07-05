"""
tests/test_notifications.py — Mixtape

Regression tests for notification logic.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_users_and_song(app):
    """Create two users and a song shared by the first user."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Test Song", artist="Test Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_sends_notification_to_sharer(app, seed_users_and_song):
    """
    When a user rates a song they didn't share, the song's sharer should
    receive a 'song_rated' notification.

    This test would have failed against the buggy code because rate_song()
    committed the rating but never called create_notification(), leaving
    the sharer's notification list empty.
    """
    with app.app_context():
        sharer_id = seed_users_and_song["sharer"].id
        rater_id = seed_users_and_song["rater"].id
        song_id = seed_users_and_song["song"].id

        rate_song(user_id=rater_id, song_id=song_id, score=4)

        notifications = get_notifications(sharer_id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"
        assert "rater" in notifications[0]["body"]
        assert "Test Song" in notifications[0]["body"]


def test_self_rating_does_not_send_notification(app, seed_users_and_song):
    """
    When the sharer rates their own song, no notification should be sent.
    """
    with app.app_context():
        sharer_id = seed_users_and_song["sharer"].id
        song_id = seed_users_and_song["song"].id

        rate_song(user_id=sharer_id, song_id=song_id, score=5)

        notifications = get_notifications(sharer_id)
        assert len(notifications) == 0
