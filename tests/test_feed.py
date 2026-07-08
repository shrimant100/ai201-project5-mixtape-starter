"""
tests/test_feed.py — Mixtape

Tests for Friends Listening Now feed logic.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def friends_with_song(app):
    with app.app_context():
        viewer = User(username="nova", email="nova@test.com")
        friend = User(username="darius", email="darius@test.com")
        db.session.add_all([viewer, friend])
        db.session.flush()
        song = Song(title="Midnight Drive", artist="Test Artist", shared_by=viewer.id)
        db.session.add(song)
        db.session.flush()
        db.session.execute(friendships.insert().values(user_id=viewer.id, friend_id=friend.id))
        db.session.execute(friendships.insert().values(user_id=friend.id, friend_id=viewer.id))
        db.session.commit()
        yield {"viewer": viewer, "friend": friend, "song": song}


def test_excludes_friend_who_listened_yesterday_within_24h(app, friends_with_song):
    """
    A friend who listened last night should not appear the next morning,
    even if the listen was fewer than 24 hours ago.
    """
    with app.app_context():
        viewer = db.session.get(User, friends_with_song["viewer"].id)
        friend = db.session.get(User, friends_with_song["friend"].id)
        song = db.session.get(Song, friends_with_song["song"].id)

        sunday_11pm = datetime(2024, 6, 16, 23, 0, 0, tzinfo=timezone.utc)
        monday_9am = datetime(2024, 6, 17, 9, 0, 0, tzinfo=timezone.utc)

        db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=sunday_11pm))
        db.session.commit()

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return monday_9am

        with patch("services.feed_service.datetime", FakeDatetime):
            feed = get_friends_listening_now(viewer.id)

        assert feed == []


def test_includes_friend_who_listened_today(app, friends_with_song):
    """A friend who listened earlier today should appear in the feed."""
    with app.app_context():
        viewer = db.session.get(User, friends_with_song["viewer"].id)
        friend = db.session.get(User, friends_with_song["friend"].id)
        song = db.session.get(Song, friends_with_song["song"].id)

        monday_8am = datetime(2024, 6, 17, 8, 0, 0, tzinfo=timezone.utc)
        monday_9am = datetime(2024, 6, 17, 9, 0, 0, tzinfo=timezone.utc)

        db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=monday_8am))
        db.session.commit()

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return monday_9am

        with patch("services.feed_service.datetime", FakeDatetime):
            feed = get_friends_listening_now(viewer.id)

        assert len(feed) == 1
        assert feed[0]["friend"]["username"] == "darius"
