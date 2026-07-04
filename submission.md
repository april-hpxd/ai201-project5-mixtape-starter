# Mixtape Bug Hunt — Submission

---

## AI Usage

I used Claude throughout this project in three distinct ways.

**1. Codebase orientation.** Before touching any bug, I fed the entire project to an AI exploration agent and asked it to summarize every file's responsibility, every function's logic, and trace the full data flow for key features (song rating → notification, listen event → streak update, search query → deduplication). The agent read models.py, all five service files, all four route files, seed_data.py, and every test file. This gave me a complete mental model in minutes rather than hours of manual reading.

**2. Explaining specific functions.** When I needed to verify my understanding of how SQLAlchemy's `.outerjoin()` behaves with many-to-many association tables, I asked the AI to explain the row-multiplication behavior — confirming that each `song_tags` row produces a separate result row before deduplication. I verified this by reading the actual query in `search_service.py` and the test comments (`"Should be 1, bug causes it to be 3"`).

**3. Verifying fixes before committing.** After reasoning through each root cause myself, I used the AI to check whether my proposed fix was the *minimal* correct change — for example, confirming that `.distinct()` was sufficient for the search bug rather than post-processing the results in Python, and confirming that the `songs[:-1]` slice in `playlist_service.py` is the canonical off-by-one pattern.

**Where I overrode or independently verified AI output:** For Bug #2 (feed threshold), the AI's initial summary described it as "possibly a timezone comparison problem." I read the actual `RECENT_THRESHOLD = timedelta(hours=24)` constant, noted that 24 hours includes events from "yesterday" by definition, checked the seed data's event timestamps (10–30 minutes ago vs. 1–14 days ago), and determined the fix was changing the constant to `timedelta(minutes=30)` — a different diagnosis than the AI's first guess.

---

## Codebase Map


### Directory Structure

```
mixtape-starter/
├── app.py                  # Flask app factory; registers blueprints
├── models.py               # All SQLAlchemy ORM models
├── seed_data.py            # Populates DB with realistic test data
├── routes/
│   ├── songs.py            # /songs endpoints (search, rate, listen)
│   ├── playlists.py        # /playlists endpoints (CRUD, songs)
│   ├── users.py            # /users endpoints (profile, streak, notifications)
│   └── feed.py             # /feed endpoints (listening-now, activity)
├── services/
│   ├── streak_service.py   # Consecutive-day listening streak logic
│   ├── feed_service.py     # Friend activity feed queries
│   ├── search_service.py   # Song search by title/artist
│   ├── notification_service.py  # Create/read/mark notifications; also owns rate_song and add_to_playlist
│   └── playlist_service.py # Playlist creation and ordered song retrieval
└── tests/
    ├── test_streaks.py
    ├── test_search.py
    └── test_playlists.py
```

### Models (models.py)

| Model | Key Columns | Role |
|-------|------------|------|
| `User` | id, username, listening_streak, last_listened_at | App users; owns streak state |
| `Song` | id, title, artist, genre, shared_by | Songs shared into the system |
| `Tag` | id, name | Genre/style labels; many-to-many with Song |
| `ListeningEvent` | id, user_id, song_id, listened_at | Each time a user listens |
| `Rating` | id, user_id, song_id, score (1–5) | One rating per user per song (UniqueConstraint) |
| `Playlist` | id, name, created_by, is_collaborative | Ordered song collections |
| `Notification` | id, user_id, notification_type, body, read | User inbox messages |

**Association tables:**
- `friendships` — symmetric User↔User many-to-many
- `song_tags` — Song↔Tag many-to-many
- `playlist_entries` — Song↔Playlist many-to-many, adds `position` (integer) and `added_by`

### Architecture Pattern

Every route does exactly two things: parse inputs, call a service. All business logic lives in `services/`. No service calls another route; `notification_service` imports from `playlist_service` in one place (checking existing songs), but otherwise each service is self-contained.

### Complete Data Flow: User rates a song

1. **HTTP:** `POST /songs/<song_id>/rate` with `{"user_id": "...", "score": 4}`
2. **Route** (`routes/songs.py → rate()`): Extracts `user_id` and `score` from JSON body, calls `notification_service.rate_song(user_id, song_id, score)`.
3. **Service** (`notification_service.py → rate_song()`):
   - Validates score is 1–5
   - Loads Song and User from DB
   - Upserts a `Rating` row (insert if new, update if existing)
   - Commits
   - If rater ≠ song sharer, calls `create_notification()` to insert a `Notification` row for the sharer
4. **DB:** One `Rating` row written; one `Notification` row written
5. **Response:** The route returns the Rating dict with HTTP 201

### Complete Data Flow: Song search

1. **HTTP:** `GET /songs/search?q=Borough`
2. **Route** (`routes/songs.py → search()`): Passes `q` to `search_service.search_songs(query)`.
3. **Service** (`search_service.py → search_songs()`): SQL query — `Song` outer-joined to `song_tags`, filtered by `ILIKE`, with `.distinct()` to collapse tag-multiplied rows.
4. **DB:** Returns `Song` ORM objects; `Song.to_dict()` resolves the `tags` relationship (subquery-loaded).
5. **Response:** `{"results": [...], "count": N}`

---

## Root Cause Analysis

---

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:**
Using the test in `tests/test_streaks.py`, I called `update_listening_streak(user, saturday)` followed by `update_listening_streak(user, sunday)` and asserted the streak should be 2. The test failed — the streak was 1 (reset) instead of 2 (incremented).

**How I found the root cause:**
I opened `services/streak_service.py` and read `update_listening_streak()`. The function computes `days_since_last = (today - last_date).days` and then branches:
- `== 0` → no change (already listened today)
- `== 1` → increment
- else → reset to 1

On line 73 I saw the actual condition: `elif days_since_last == 1 and today.weekday() != 6:`. The extra guard `today.weekday() != 6` means "only increment if today is NOT Sunday." Python's `datetime.weekday()` returns 0=Monday … 6=Sunday, so any consecutive-day listen that lands on a Sunday hits the `else` branch instead and resets the streak to 1.

**Root cause:**
`datetime.weekday()` returns 6 for Sunday. The condition `today.weekday() != 6` prevents streak increment whenever today is Sunday. A user who listened on Saturday and then Sunday had `days_since_last == 1` (correct) but `today.weekday() == 6` (Sunday), so the `elif` was False and the `else` branch executed, resetting the streak to 1 instead of incrementing it.

**Fix and side-effect check:**
Removed `and today.weekday() != 6` from line 73, leaving `elif days_since_last == 1:`. This is the only condition needed — the `days_since_last` computation already correctly identifies consecutive days regardless of weekday. All four other streak tests (new user, consecutive non-Sunday, same-day double, skipped day) were run and still pass — the fix touches only this one guard.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it:**
With the seeded database, calling `GET /feed/<nova_id>/listening-now` returned friend events that were 20+ hours old (from the previous day), alongside genuinely recent events. The endpoint claims to show who is listening "now" but showed stale activity.

**How I found the root cause:**
I opened `services/feed_service.py`. The cutoff is computed as:
```python
RECENT_THRESHOLD = timedelta(hours=24)
cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD
```
Any `ListeningEvent` with `listened_at >= cutoff` is included. A 24-hour window is correct for "recent activity," but "Friends Listening Now" implies real-time presence — not whether someone listened at any point in the last day. The seed data has listening events from 1 day ago and 14 days ago; the 24-hour threshold included the 1-day-ago events. I confirmed the seed data creates three "recent" events 10–30 minutes ago, which is the correct population for "listening now."

**Root cause:**
`RECENT_THRESHOLD = timedelta(hours=24)` was too broad. A user who listened 23 hours ago (yesterday evening) satisfied `listened_at >= cutoff` and appeared as "listening now." The feature semantics require a tight real-time window, not a 24-hour lookback.

**Fix and side-effect check:**
Changed `RECENT_THRESHOLD = timedelta(hours=24)` to `RECENT_THRESHOLD = timedelta(minutes=30)`. This excludes yesterday's events while including genuine recent activity. `get_activity_feed()` in the same file does NOT use `RECENT_THRESHOLD` — it intentionally has no time window — so it is unaffected. The constant name and the query logic are unchanged; only the value changes.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:**
With seed data loaded, `GET /songs/search?q=Crown+Heights` returned the same song ("Crown Heights Anthem") three times. That song has three tags (rap, hip-hop, boom bap). A song with one tag appeared once; a song with no tags appeared once. The `test_search_no_duplicates_multi_tag_song` test confirmed: expected 1, got 3.

**How I found the root cause:**
I opened `services/search_service.py → search_songs()`. The query:
```python
db.session.query(Song)
    .outerjoin(song_tags, Song.id == song_tags.c.song_id)
    .filter(...)
    .all()
```
An SQL outer join between `Song` and `song_tags` produces one result row per `song_tags` entry. A song with 3 tags generates 3 rows in the joined result set. SQLAlchemy's `query(Song)` maps each row to a Song ORM object — so 3 identical Song objects are returned. `[song.to_dict() for song in results]` then serializes all three, producing three identical dicts.

**Root cause:**
The `outerjoin` on `song_tags` is needed to allow filtering by tag, but without `.distinct()`, each tag match produces a separate copy of the song in the result. The number of duplicates equals the number of matching tags on the song.

**Fix and side-effect check:**
Added `.distinct()` before `.all()`. SQLAlchemy adds `SELECT DISTINCT` to the SQL, collapsing duplicate `Song` rows. Songs with zero tags or one tag are unaffected (they already produced one row). The `Song.to_dict()` call resolves the `tags` relationship via a subquery (configured in the model), so the tag list is still complete even after deduplication at the query level. All five search tests pass.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**How I reproduced it:**
I called `POST /songs/<song_id>/rate` with a different user's ID than the sharer. Then I checked `GET /users/<sharer_id>/notifications` — the list was empty. Repeating the same test with `add_to_playlist` produced a notification as expected.

**How I found the root cause:**
I read `notification_service.py` and compared `add_to_playlist()` and `rate_song()` side by side. `add_to_playlist()` ends with:
```python
if song.shared_by != added_by_user_id:
    create_notification(user_id=song.shared_by, ...)
```
`rate_song()` has no such block — it commits the rating and immediately returns. The notification logic was simply never added to `rate_song()`. Both functions are in the same file; the `create_notification()` helper exists and is tested. The omission is architectural: the rating path was implemented without mirroring the notification pattern that the playlist-add path uses.

**Root cause:**
`rate_song()` in `notification_service.py` committed the `Rating` row but never called `create_notification()`. The notification for "song rated" was never created, so the song sharer had no way to know their song received a rating.

**Fix and side-effect check:**
Added notification creation after `db.session.commit()` in `rate_song()`, using the same guard (`song.shared_by != user_id`) as `add_to_playlist()`:
```python
if song.shared_by != user_id:
    create_notification(
        user_id=song.shared_by,
        notification_type="song_rated",
        body=f"{rater.username} rated your song '{song.title}' {score}/5.",
    )
```
Side-effect checks: (1) Self-rating produces no notification (guard prevents it). (2) Updating an existing rating still triggers a notification for the sharer — this is intentional, since the sharer may want to know about score changes. (3) The `get_notifications()` and `mark_as_read()` functions are unmodified. Regression test in `tests/test_notifications.py` verifies both the notification-sent and no-self-notification cases.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:**
With a seeded playlist of 5 songs, `GET /playlists/<id>/songs` returned only 4 songs. The `test_playlist_returns_all_songs` test asserted `len(songs) == 5` and failed with 4.

**How I found the root cause:**
I opened `services/playlist_service.py → get_playlist_songs()`. The SQL query correctly joins `playlist_entries`, filters by `playlist_id`, and orders by `position` ascending. Then line 66:
```python
return [song.to_dict() for song in songs[:-1]]
```
Python's `[:-1]` slice returns all elements except the last. With 5 songs, this returns songs 1–4 and silently drops song 5 (the one at the highest position). An empty playlist returns `[][:-1]` which is `[]` — so the edge case accidentally works, hiding the bug for empty playlists.

**Root cause:**
`songs[:-1]` is an off-by-one slice error that always excludes the last song in the ordered result. Because the query sorts by `position` ascending, the dropped song is always the one assigned the highest position number — the last track in the playlist.

**Fix and side-effect check:**
Changed `songs[:-1]` to `songs` on line 66. This is a single character-range deletion with no logic change — the query, ordering, and serialization are all correct; only the slice was wrong. Verified: (1) 5-song playlist returns 5 songs in order — passes. (2) Empty playlist returns `[]` — still passes, because `[][:]` is `[]`. (3) `get_playlist()` (metadata only) and `get_user_playlists()` are in the same file and are unaffected.


---

## Git Commit History

| # | Commit message | Bug |
|---|---------------|-----|
| 1 | `fix: remove incorrect Sunday exclusion from streak increment logic` | Issue #1 |
| 2 | `fix: reduce Friends Listening Now threshold from 24 hours to 30 minutes` | Issue #2 |
| 3 | `fix: add distinct() to search query to prevent duplicate results for multi-tag songs` | Issue #3 |
| 4 | `fix: send notification to song sharer when their song is rated` | Issue #4 |
| 5 | `fix: remove [:-1] slice that dropped the last song from every playlist` | Issue #5 |
