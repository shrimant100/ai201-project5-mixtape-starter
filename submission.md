# Mixtape Bug Hunt — Submission

## Codebase Map

### Overview

Mixtape is a Flask + SQLAlchemy social music app. It follows a **routes → services → models** pattern: routes handle HTTP parsing and JSON responses; all business logic lives in `services/`; `models.py` defines the database schema.

### Main Files


| File                               | Role                                                                                                                                                            |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`                           | Flask app factory (`create_app`). Initializes SQLAlchemy, registers four blueprints (`/songs`, `/playlists`, `/users`, `/feed`), and creates tables on startup. |
| `models.py`                        | Defines 7 SQLAlchemy models plus 3 association tables.                                                                                                          |
| `routes/songs.py`                  | Song search, detail, rating (`POST /songs/<id>/rate`), and listening events (`POST /songs/<id>/listen`).                                                        |
| `routes/playlists.py`              | Playlist CRUD and adding songs to playlists.                                                                                                                    |
| `routes/users.py`                  | User profiles, streak lookup, and notification retrieval.                                                                                                       |
| `routes/feed.py`                   | "Friends Listening Now" and general activity feed.                                                                                                              |
| `services/streak_service.py`       | Records listening events and updates consecutive-day streaks.                                                                                                   |
| `services/feed_service.py`         | Builds friend activity feeds from `ListeningEvent` records.                                                                                                     |
| `services/search_service.py`       | Case-insensitive song search by title or artist.                                                                                                                |
| `services/notification_service.py` | Creates notifications for social interactions; also handles rating and playlist-add side effects.                                                               |
| `services/playlist_service.py`     | Playlist creation and ordered song retrieval.                                                                                                                   |
| `seed_data.py`                     | Seeds 5 users, 25 songs, 3 playlists, listening events, and friendships for local testing.                                                                      |


### Data Model (`models.py`)

Seven models and three join tables:

- **User** — username, email, `listening_streak`, `last_listened_at`. Many-to-many friendships via `friendships` table.
- **Song** — title, artist, album, genre, `shared_by` (FK to User). Many-to-many tags via `song_tags`.
- **Tag** — genre/category labels attached to songs.
- **ListeningEvent** — records when a user listened to a song (`user_id`, `song_id`, `listened_at`). Drives streaks and feeds.
- **Rating** — one rating per user per song (1–5), enforced by a unique constraint.
- **Playlist** — name, creator, collaborative flag. Songs linked via `playlist_entries` join table, which includes a `position` column for ordering.
- **Notification** — `notification_type`, `body`, `read` flag, sent to a `user_id`.

### Organizational Patterns

1. **Thin routes, fat services** — Routes validate request params, call one service function, and return JSON. They never touch the database directly (except `routes/users.py` for a simple user lookup).
2. **Side effects bundled in services** — `notification_service.rate_song()` saves the rating; `notification_service.add_to_playlist()` both adds the song and creates a notification. This is the pattern to compare when debugging Issue #4.
3. **Errors via `ValueError`** — Services raise `ValueError` for missing entities or bad input; routes catch these and return 400/404.
4. `**to_dict()` on models** — Every model has a `to_dict()` method for JSON serialization. Services return dicts (or model instances that routes call `.to_dict()` on).

### Data Flow: Rating a Song (Issue #4 territory)

```
POST /songs/<song_id>/rate
  → routes/songs.py :: rate()
    → parses JSON body: { user_id, score }
    → services/notification_service.py :: rate_song(user_id, song_id, score)
      → validates score (1–5), fetches Song and User
      → upserts a Rating record (update if exists, create if new)
      → db.session.commit()
      → returns Rating instance
    → route returns rating.to_dict() as JSON 201
```

For comparison, the **working** playlist-add notification flow:

```
POST /playlists/<playlist_id>/songs
  → routes/playlists.py :: add_song()
    → services/notification_service.py :: add_to_playlist(playlist_id, song_id, added_by)
      → validates song, user, playlist exist
      → appends song to playlist.songs if not already present
      → if added_by ≠ song.shared_by:
          → create_notification(user_id=song.shared_by, type="song_added_to_playlist", body=...)
      → returns None (route returns 201 message)
```

The key difference: `add_to_playlist` calls `create_notification()` after the main action, but `rate_song` does not.

### Data Flow: Recording a Listen (Issue #1 territory)

```
POST /songs/<song_id>/listen
  → routes/songs.py :: listen()
    → services/streak_service.py :: record_listening_event(user_id, song_id)
      → creates ListeningEvent with current UTC timestamp
      → calls update_listening_streak(user, now)
        → compares today's date to last_listened_at date
        → increments streak if yesterday, resets if gap > 1 day, no-op if same day
      → commits and returns event
```

Streak is read separately via `GET /users/<user_id>/streak` → `streak_service.get_streak()`.

### Data Flow: Friends Listening Now (Issue #2 territory)

```
GET /feed/<user_id>/listening-now
  → routes/feed.py :: listening_now()
    → services/feed_service.py :: get_friends_listening_now(user_id)
      → loads user's friends
      → queries ListeningEvents from friends where listened_at >= (now - 24 hours)
      → deduplicates to one entry per friend (most recent)
      → returns list of { friend, song, listened_at } dicts
```

---

## Bug Fix Plan

I've read all five issue descriptions. I plan to fix these three in issue order:


| Priority | Issue                                                      | Why                                                                                                                    |
| -------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 1        | **#1 — Listening streak resets on Sundays**                | Date/time logic bug in `streak_service.py`; good starting point since the listen → streak data flow is already mapped. |
| 2        | **#2 — Friends Listening Now shows people from yesterday** | Feed uses a rolling 24-hour window instead of filtering to today's activity; fix in `feed_service.py`.                 |
| 3        | **#3 — The same song keeps showing up twice in search**    | Search duplicates are conditional on tag count; requires tracing the join logic in `search_service.py`.                |


Stretch goals if time allows: **#4** (missing rating notification — architectural pattern comparison) and **#5** (last playlist song never shows up).

---

## Issue #1 — Listening streak resets on Sundays

**Affected service:** `services/streak_service.py`  
**Reported by:** kenji

### How I Reproduced It

#### Trigger conditions

This bug is **Sunday-only**. It fires when all of the following are true:

- The user has an active streak (`listening_streak` > 1).
- The user's `last_listened_at` date is **yesterday** (`days_since_last == 1`).
- **Today is Sunday** (`datetime.weekday() == 6`).

On any other weekday, consecutive-day listening increments the streak normally. The bug does not appear if a day was skipped (`days_since_last > 1`) or if the user already listened today (`days_since_last == 0`).

#### Data state required

- A user with `listening_streak = 12` and `last_listened_at` set to the previous calendar day (Saturday evening).
- A valid `song_id` to record a listen event against.

Seed data includes **kenji** with `listening_streak = 12`, but kenji's `last_listened_at` is set to "today" in the seed — so to hit the Saturday → Sunday path, the user's `last_listened_at` must be adjusted to yesterday before listening on a Sunday.

#### Steps to reproduce

**Method A — via pytest (controlled dates, works any day):**

```bash
pytest tests/test_streaks.py::test_streak_increments_on_sunday -v
```

**Method B — via service layer (controlled dates, works any day):**

1. Create a user with `listening_streak = 12` and `last_listened_at = 2024-06-15` (Saturday).
2. Call `update_listening_streak(user, sunday_datetime)` where `sunday_datetime` is `2024-06-16` (Sunday).
3. Read `user.listening_streak`.

**Method C — via API endpoints (requires running on an actual Sunday):**

1. Start the app: `$env:FLASK_APP = "app:create_app"; flask run`
2. Set kenji's `last_listened_at` to Saturday (direct DB update or listen on Saturday).
3. `POST /songs/<song_id>/listen` with body `{ "user_id": "<kenji_id>" }` on Sunday morning.
4. `GET /users/<kenji_id>/streak`

#### Inputs used


| Input              | Value                                                                      |
| ------------------ | -------------------------------------------------------------------------- |
| User               | kenji (`1149a09f-b5af-46d0-8d5b-b79472dca53c`) or test user with streak 12 |
| `last_listened_at` | Saturday 2024-06-15 20:00 UTC                                              |
| Listen timestamp   | Sunday 2024-06-16 09:00 UTC                                                |
| `days_since_last`  | 1                                                                          |
| `today.weekday()`  | 6 (Sunday)                                                                 |


#### Sequence of actions

1. User listens daily through Saturday → streak reaches 12, `last_listened_at` = Saturday.
2. User listens again Sunday morning → `record_listening_event` calls `update_listening_streak`.
3. Streak logic sees `days_since_last == 1` but also checks `today.weekday() != 6`, which fails on Sunday.
4. Control falls through to the `else` branch, resetting streak to 1.

#### Expected vs actual


|              | Value                          |
| ------------ | ------------------------------ |
| **Expected** | Streak increments from 12 → 13 |
| **Actual**   | Streak resets to 1             |


#### Reproduction output

```
BEFORE Sunday listen: streak=12, last_listened=2024-06-15
AFTER Sunday listen:  streak=1 (expected 13)
days_since_last=1, today.weekday()=6
```

Pytest failure confirming the same behavior:

```
tests/test_streaks.py::test_streak_increments_on_sunday FAILED
assert 1 == 2   # streak stays 1 instead of incrementing to 2
```

#### Root cause

In `update_listening_streak`, line 73 checks `days_since_last == 1 and today.weekday() != 6`. The `weekday() != 6` guard incorrectly excludes Sunday from consecutive-day increment, sending Sunday listeners to the reset branch on line 76.

**Confirmed:** Yes — `today.weekday() != 6` is the culprit. On Sunday, `days_since_last == 1` is true but the combined condition is false, so the `else` branch runs and resets the streak to 1. No other code path is involved.

#### Fix

Remove the erroneous Sunday exclusion so consecutive-day logic applies uniformly:

```python
# Before (buggy)
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1

# After (fixed)
elif days_since_last == 1:
    user.listening_streak += 1
```

**File changed:** `services/streak_service.py`, line 73.

#### Post-fix verification

```bash
pytest tests/test_streaks.py -v
```

```
tests/test_streaks.py::test_streak_starts_at_1_for_new_user PASSED
tests/test_streaks.py::test_streak_increments_on_consecutive_day PASSED
tests/test_streaks.py::test_streak_does_not_double_count_same_day PASSED
tests/test_streaks.py::test_streak_resets_after_skipped_day PASSED
tests/test_streaks.py::test_streak_increments_on_sunday PASSED

5 passed in 0.86s
```

---

## AI Tool Disclosure

AI was used during codebase orientation to summarize service files, trace data flows, and draft this codebase map. All bug fixes will be implemented and verified manually.