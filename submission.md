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

---

## Git Commit History

| # | Commit message | Bug |
|---|---------------|-----|
| 1 | `fix: remove incorrect Sunday exclusion from streak increment logic` | Issue #1 |
