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

## Issue #2 — Friends Listening Now shows people from yesterday

**Affected service:** `services/feed_service.py`  
**Reported by:** nova

### How I Reproduced It

#### Trigger conditions

This bug appears on **morning-after** scenarios when all of the following are true:

- The viewing user has at least one friend with a recent `ListeningEvent`.
- The friend's most recent listen was **last night** (previous calendar day).
- The viewer checks the feed **this morning**, within 24 hours of that listen (e.g. 11 PM yesterday → 9 AM today = 10 hours ago).

The bug does **not** appear if the friend listened earlier than 24 hours ago, or if the friend listened today. It is most visible when the listen crosses a **calendar-day boundary** but still falls inside the rolling 24-hour window.

#### Data state required

- A viewing user (nova) with at least one friend (darius).
- A `ListeningEvent` for the friend with `listened_at` set to the previous evening (e.g. Sunday 11:00 PM UTC).
- Current time simulated or actual as the following morning (e.g. Monday 9:00 AM UTC).

Seed data sets up nova ↔ darius as friends and includes recent listening events, but those events are only minutes old — so to reproduce the morning-after case, the friend's `listened_at` must be set to yesterday evening.

#### Steps to reproduce

**Method A — via service layer with controlled time (works any day):**

1. Create a viewer and friend with a bidirectional friendship.
2. Insert a `ListeningEvent` for the friend at yesterday 11:00 PM UTC.
3. Patch `datetime.now()` to return today 9:00 AM UTC.
4. Call `get_friends_listening_now(viewer_id)`.
5. Inspect the returned feed count and `listened_at` dates.

**Method B — via API endpoints:**

1. Start the app: `$env:FLASK_APP = "app:create_app"; flask run`
2. Update darius's most recent `ListeningEvent` `listened_at` to yesterday evening (or wait until morning after a late-night listen).
3. `GET /feed/<nova_id>/listening-now`
4. Check whether darius appears despite not having listened today.

#### Inputs used

| Input | Value |
|-------|-------|
| Viewer | nova (`369e7205-133b-4102-bcdd-8c26d9c50731`) |
| Friend | darius (`6539e12c-cfde-4574-981b-0064548d47d5`) |
| Friend `listened_at` | 2024-06-16 23:00 UTC (previous day) |
| Viewer checks feed at | 2024-06-17 09:00 UTC (next morning) |
| Hours since listen | 10 |
| `RECENT_THRESHOLD` | `timedelta(hours=24)` |

#### Sequence of actions

1. Friend darius listens to a song at 11:00 PM Sunday night → `ListeningEvent` created.
2. Viewer nova opens "Friends Listening Now" at 9:00 AM Monday morning.
3. `get_friends_listening_now` computes `cutoff = now - 24 hours` (Monday 9 AM − 24h = Sunday 9 AM).
4. Darius's event at Sunday 11 PM is **after** the cutoff → included in feed.
5. Feed shows darius as "listening now" even though his last listen was **yesterday**, not today.

#### Expected vs actual

| | Value |
|---|-------|
| **Expected** | Feed is empty (or excludes darius) — only friends who listened **today** should appear |
| **Actual** | Feed contains 1 entry for darius with `listened_at` from yesterday |

#### Reproduction output

```
friend_listened_at=2024-06-16 23:00:00+00:00 (2024-06-16)
viewer_checks_at=2024-06-17 09:00:00+00:00 (2024-06-17)
hours_since_listen=10.0
24h_cutoff=2024-06-16 09:00:00+00:00
within_24h_window=True
same_calendar_day=False
feed_count=1
friend_in_feed=friend2
listened_at=2024-06-16T23:00:00
expected_count=0 (friend listened yesterday, not today)
```

#### Root cause

**Confirmed: the rolling 24-hour cutoff is the primary driver.**

In `get_friends_listening_now`, line 32 computes:

```python
cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD  # RECENT_THRESHOLD = timedelta(hours=24)
```

The filter `ListeningEvent.listened_at >= cutoff` includes any event from the past 24 hours **regardless of calendar day**. That is why a friend who listened at 11 PM yesterday still appears at 9 AM today — only 10 hours have passed, so the event clears the cutoff even though it is not from "today."

Our reproduction proved this directly:

| Check | Result |
|-------|--------|
| `within_24h_window` | `True` → friend incorrectly included |
| `same_calendar_day` (UTC) | `False` → should have been excluded |

**Timezone is a secondary factor, not the root bug.**

The entire app stores and compares timestamps in **UTC** (`datetime.now(timezone.utc)` in `feed_service.py`, `streak_service.py`, and `seed_data.py`). There is no user timezone field or local-time conversion anywhere. That means:

- Users perceive "today" in their **local** timezone.
- The feed evaluates recency in **UTC**.

Example — same wall-clock scenario in US Pacific (11 PM Sunday → 9 AM Monday local):

```
friend_listened_utc=2024-06-17 06:00:00+00:00   # Monday in UTC
viewer_checks_utc=2024-06-17 16:00:00+00:00     # also Monday in UTC
local_same_calendar_day=False                    # Sunday vs Monday locally
utc_same_calendar_day=True                       # both Monday in UTC
rolling_24h_includes=True                        # still included (10 hours ago)
```

So UTC/local misalignment can make "listening now" feel wrong to users even beyond this bug — but for Issue #2 specifically, the **incorrect filter type** (rolling 24-hour window instead of calendar-day boundary) is what the code is doing wrong. The intended behavior ("only friends who listened today") matches how `streak_service.py` already handles dates: compare `listened_at.date()` against `now.date()` in UTC.

**Fix direction:** Replace the rolling cutoff with a UTC calendar-day filter, consistent with streak logic.

#### Fix

Replace the rolling 24-hour cutoff with a UTC calendar-day window:

```python
# Before (buggy)
cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD
ListeningEvent.listened_at >= cutoff

# After (fixed)
now = datetime.now(timezone.utc)
start_of_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
start_of_tomorrow = start_of_today + timedelta(days=1)
ListeningEvent.listened_at >= start_of_today,
ListeningEvent.listened_at < start_of_tomorrow,
```

Also removed the unused `RECENT_THRESHOLD` constant.

**File changed:** `services/feed_service.py`, lines 13 and 29–42.

#### Post-fix verification

```bash
pytest tests/test_feed.py tests/test_streaks.py tests/test_search.py -v
```

```
tests/test_feed.py::test_excludes_friend_who_listened_yesterday_within_24h PASSED
tests/test_feed.py::test_includes_friend_who_listened_today PASSED
tests/test_streaks.py (5 tests) PASSED
tests/test_search.py (5 tests) PASSED

12 passed in 1.07s
```

Note: `tests/test_playlists.py` still fails — that is the separate Issue #5 bug, not yet fixed.

---

## AI Tool Disclosure

AI was used during codebase orientation to summarize service files, trace data flows, and draft this codebase map. All bug fixes will be implemented and verified manually.