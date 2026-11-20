# LinkedIn Games Data Collector

Collects your daily LinkedIn Games scores (Zip, Tango, Queens, Mini Sudoku, Patches,
CrossClimb, Wend, Pinpoint, Sumbreak) with Playwright and BeautifulSoup, keeps them in a local
JSON store that imports cleanly into Pandas, and can export a CSV for Google Sheets or
Excel. An optional service also plays the day's games, reads your connections'
leaderboards and posts a summary to a Slack channel.

## Why this exists

LinkedIn Games are a nice daily break, and they are more fun when a team compares
results. This project turns them into a small office game: every day it gathers the
scores of everyone in a company or friend group, builds a ranked leaderboard per game
(with a monkey emoji for everyone outside the top 3) and shares it in a Slack channel.
It was built for company teams that want to play LinkedIn Games together, not as a
serious analytics tool.

> **Disclaimer.** This is an unofficial tool and is not affiliated with LinkedIn.
> Automating a LinkedIn account (logging in with a browser, and especially the
> automatic game playing in `service.py`) may violate LinkedIn's User Agreement and
> can get the account restricted. Use it at your own risk, preferably with an account
> you can afford to lose.

## Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Connecting your LinkedIn account](#3-connecting-your-linkedin-account)
4. [Configuration](#4-configuration)
5. [Collecting scores](#5-collecting-scores)
6. [Slack integration](#6-slack-integration)
7. [The bot player](#7-the-bot-player)
8. [Default settings](#8-default-settings)
9. [Running it every day](#9-running-it-every-day)
10. [Use as a Claude Code skill](#10-use-as-a-claude-code-skill)
11. [Privacy and what never gets committed](#11-privacy-and-what-never-gets-committed)
12. [Troubleshooting](#12-troubleshooting)

Deeper reference (every config key, CLI flag, CSV/Google Sheets sync, time zones,
known limitations) lives in [SETUP.md](SETUP.md).

## 1. Requirements

- Python 3.10 or later
- A LinkedIn account that plays the daily games
- Optional: a Slack workspace where you can add an Incoming Webhook

## 2. Installation

```bash
git clone <this-repo-url> linkedin-games
cd linkedin-games/scripts

python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -r requirements.txt
playwright install chromium
```

## 3. Connecting your LinkedIn account

The scripts never see your password. You log in once in a real browser window and the
resulting session is saved encrypted on your machine.

```bash
python setup_auth.py
```

1. A Chromium window opens on the LinkedIn login page.
2. Log in normally, including any 2FA or captcha prompt.
3. When you reach your LinkedIn feed, go back to the terminal and press Enter.

What is stored and where:

| What | Where |
|------|-------|
| Encrypted session (`linkedin_state.enc`) | macOS `~/Library/Application Support/linkedin-games/`, Linux `~/.local/share/linkedin-games/`, Windows `%LOCALAPPDATA%\linkedin-games\` |
| Key that decrypts it | OS credential store (Keychain, Credential Manager, Secret Service) |

Both live outside the project folder, so they cannot end up in a commit.

On a headless server or in Docker there is no credential store. Generate a key and put
it in `scripts/.env` instead:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```
LINKEDIN_GAMES_MASTER_KEY=<the generated key>
```

The browser login still has to happen once on a machine with a display; copy the `.enc`
file and the same key to the server afterwards.

When the session expires (LinkedIn logs you out after a while) runs fail with a login
error. Run `python setup_auth.py` again. To remove the saved session:
`python setup_auth.py --delete`.

## 4. Configuration

The first run copies `scripts/config.json.sample` to `scripts/config.json`. That file is
git-ignored, so your edits stay local. You can also create it yourself before the first
run:

```bash
cp config.json.sample config.json
```

The keys you will normally change:

| Key | Meaning |
|-----|---------|
| `my_name` | Your name as it should appear in leaderboards and Slack messages. LinkedIn labels you "You" or "Siz"; this replaces that label. **Replace the `Your Name` placeholder.** |
| `track_connections` | `true` to also read your connections' leaderboards (needed for Slack). |
| `friends_filter` | Optional list of names. When it is not empty, only those connections are shown. |
| `games` | Games to collect, in column order. Valid keys: `zip`, `tango`, `queens`, `patches`, `mini_sudoku`, `crossclimb`, `wend`, `pinpoint`, `sumbreak`. |
| `anchor_game` | The game you play first each day. It must be in `games`. `pinpoint` is the safest choice because it has no timer. |
| `output_path` | Directory for `results.json`, `results.csv` and `leaderboard.json`. Use an absolute path or one starting with `~` so scheduled runs always write to the same place. |
| `export_csv_on_run` | `true` regenerates the CSV after every run. |

See [SETUP.md](SETUP.md#settings-in-configjson) for the full list.

## 5. Collecting scores

```bash
cd scripts
source .venv/bin/activate

python collector.py --dry-run          # fetch and print, write nothing
python collector.py --summary-only     # normal daily run, compact output
python collector.py --update           # re-fetch everything
python collector.py --export-csv       # also write the CSV
```

Output files (in `output_path`): `results.json` (your scores), `results.csv` (derived
view) and `leaderboard.json` (connections' scores, when `track_connections` is on).

Note that LinkedIn only shows a connections leaderboard to an account that has already
finished that game today.

## 6. Slack integration

`service.py` runs the full pipeline and posts one message per day to Slack through an
Incoming Webhook. The webhook URL is the only Slack credential needed; no bot token and
no OAuth.

### 6.1 Create the webhook

1. Open <https://api.slack.com/apps> and choose **Create New App**, then **From scratch**.
2. Name it (for example "LinkedIn Games") and pick your workspace.
3. In the app settings open **Incoming Webhooks** and switch it **On**.
4. Click **Add New Webhook to Workspace**, choose the target channel and allow it.
5. Copy the webhook URL. It looks like
   `https://hooks.slack.com/services/T000.../B000.../XXXX...`

Workspace admins may need to approve the app first.

### 6.2 Store the URL locally

```bash
cd scripts
cp .env.example .env
```

Edit `.env` and uncomment the line:

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T000.../B000.../XXXX...
```

`.env` is git-ignored. Anyone with this URL can post to your channel, so never paste it
into an issue, a commit or a chat.

Also make sure `config.json` has `"track_connections": true` and your `my_name`.

### 6.3 Run it

```bash
python service.py --message-only   # print the formatted message, nothing else
python service.py --dry-run        # play + collect, print the message, do not post
python service.py                  # play + collect + post to Slack
python service.py --no-play        # collect + post (you played the games by hand)
python service.py --slack-only     # post the existing leaderboard.json again
```

Start with `--message-only`, then `--dry-run`, then a real run.

`service.py` plays the day's games automatically unless you pass `--no-play`. That is
what unlocks the connections leaderboards, but it is also the part most likely to
violate LinkedIn's terms (see the disclaimer). If you play your games yourself, always
use `--no-play`.

If `SLACK_WEBHOOK_URL` is not set, the message is printed instead of posted.

### 6.4 Optional: heads-up message before the results

The webhook accepts plain POST requests, so a reminder is one line (for example half an
hour before the results run, see the next section):

```bash
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":"Results will be shared in 30 minutes."}' \
  "$SLACK_WEBHOOK_URL"
```

## 7. The bot player

LinkedIn only shows the connections leaderboard of a game to an account that has
**already finished that game today**. If the account has not played a game, its
leaderboard cannot be read and the game is missing from the results. To avoid empty
leaderboards, `service.py` first runs a small bot (`play.py`) that plays the games the
account has not finished yet, and only then collects the leaderboards.

How it behaves:

- It plays on the account saved by `setup_auth.py`, the same account that is collected.
- Games that are already finished today, whether you played them by hand or the bot did
  earlier, are skipped. The bot never replays a game and, for those games, only reads the
  results. It checks this by opening the game's results page before it starts.
- It plays fast. Each game is played over a target of **30 seconds** (random pauses
  between moves, about 0.6x to 1.4x of the average). Eight games take a few minutes
  including page loading, so the results are ready soon after the run starts.
- Every game it plays is written to `scripts/bot_played.json`. For those games your own
  entry is **removed from the leaderboard** in the terminal output and in the Slack
  message, because a bot time is not a real result. The players below you move up one
  rank (a player who was 4th becomes 3rd). If you were the only entry for a game, that
  game is left out of the message. Games you played yourself stay in the list. The same
  rule applies to the CSV written by `leaderboard.py --export-csv`.
- Your personal files (`results.json`, `results.csv`) are a score tracker for your own
  account and still contain the time the bot achieved.
- Change the pace with `python service.py --play-duration 60` or `python play.py
  --game all --duration 60`. Skip the bot completely with `python service.py
  --no-play` (use this if you play your own games).

Reminder: automatic playing may violate LinkedIn's terms (see the disclaimer).

## 8. Default settings

Values used when a key is missing from `scripts/config.json`:

| Setting | Default |
|---------|---------|
| `include_day_of_week` | `true` |
| `include_puzzle_numbers` | `false` |
| `export_csv_on_run` | `false` |
| `track_connections` | `false` (set it to `true` for leaderboards and Slack) |
| `friends_filter` | empty, so all connections are shown |
| `my_name` | not set, so LinkedIn's own "You" label is kept |
| `output_path` | not set, so the directory you run the script from |
| `output_json` / `output_csv` / `output_leaderboard` | `results.json` / `results.csv` / `leaderboard.json` |
| `anchor_game` | not set, so the first game in `games` is used |
| `games` | no default, list the games you want (see below) |

The shipped `config.json.sample` (copied to `config.json` on the first run) overrides
several of these: day of week, puzzle numbers, `export_csv_on_run` and
`track_connections` are `true`, `output_path` is `./`, `anchor_game` is `zip`, `my_name`
is `Your Name`, and `games` is `zip, tango, queens, mini_sudoku, patches, crossclimb,
wend, pinpoint, sumbreak`.

Command-line and behavior defaults:

| Behavior | Default |
|----------|---------|
| Bot pace (`--duration`) | 30 seconds per game |
| Games the bot plays (`play.py --game`) | none, you must pass `all` or one game; `service.py` always uses `all` |
| `collector.py` | smart mode, fetches only what is missing; `--update` re-fetches everything |
| `service.py` | plays unfinished games, then runs the collector with `--update`, then posts to Slack |
| Slack without `SLACK_WEBHOOK_URL` | the message is printed instead of posted |
| Leaderboard date | today, in US Pacific time (LinkedIn resets at midnight Pacific) |
| Monkey emoji | shown for every rank below 3rd place |
| Your entry in games the bot played | removed from the leaderboard, ranks below shift up by one |
| Slack and terminal language | English |

## 9. Running it every day

LinkedIn resets the games at midnight US Pacific time. Schedule the run for a time after
everyone has played, in your local time zone.

Example crontab (macOS and Linux), results at 16:30 and a reminder at 16:00. Replace the
path with your own and add `SLACK_WEBHOOK_URL` to `scripts/.env`:

```cron
0 16 * * *  cd /path/to/linkedin-games/scripts && set -a && . ./.env && set +a && curl -s -X POST -H 'Content-type: application/json' --data '{"text":"Results will be shared in 30 minutes."}' "$SLACK_WEBHOOK_URL" >/dev/null
30 16 * * * cd /path/to/linkedin-games/scripts && .venv/bin/python service.py --no-play >> service.log 2>&1
```

The computer has to be on and logged in at that time, because the browser session lives
in your user account. On macOS a `launchd` plist works the same way. `service.log` is
git-ignored.

## 10. Use as a Claude Code skill

Clone the repository to `~/.claude/skills/linkedin-games-data-collector`. The included
`skill.md` lets Claude Code run the collector for you.

## 11. Privacy and what never gets committed

Nothing personal ships with the repository. These stay on your machine and are
git-ignored:

- `scripts/config.json` (your name and settings)
- `scripts/.env` (Slack webhook, optional master key)
- `scripts/results.json`, `results.csv`, `leaderboard.json`, `bot_played.json` (your
  scores and your connections' names and scores)
- `scripts/debug/` (screenshots and HTML dumps; these contain names and session data)
- `scripts/research/`, `.venv/`, `__pycache__/`
- The encrypted LinkedIn session, which is stored outside the project folder

The leaderboard data contains the real names of your LinkedIn connections. Do not publish
it or paste it into issues. If you open a bug report, redact names and never attach files
from `debug/`.

Before publishing your own fork, run this from the repository root:

```bash
git status --short                      # nothing under scripts/ except code and samples
git ls-files | grep -E '\.(json|env|enc|csv|html|png)$'   # should only list config.json.sample and .env.example
git grep -n -i -E 'hooks\.slack\.com/services/T[A-Z0-9]{8}|xox[abp]-|li_at'
```

## 12. Troubleshooting

| Symptom | Fix |
|---------|-----|
| "No saved session" or a login error | Run `python setup_auth.py` again. |
| A game shows no score or no leaderboard | The account has not finished that game today. Play it, then run with `--update`. |
| Wrong or missing data | Run with `--debug`, then look at the screenshots and HTML in `scripts/debug/` (private, do not share). |
| Slack message does not appear | Check `SLACK_WEBHOOK_URL` in `scripts/.env`, and that the app is still installed in the workspace. Try `python service.py --slack-only`. |
| Keyring errors on a server | Set `LINKEDIN_GAMES_MASTER_KEY` in `.env` (section 3). |
| Run happens at the wrong date | See "Time Zones" in [SETUP.md](SETUP.md#time-zones). |

More in [SETUP.md](SETUP.md#troubleshooting).
