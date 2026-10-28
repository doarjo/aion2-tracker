# AION 2 Tracker

Reads your AION 2 Global character from the public PLAYNC endpoints, keeps a
history, and tells you what to fix next. Self-hosted, read-only, rate limited.

Current phase: **read-only recommendations**. An interactive what-if editor
comes later.

---

## What it does

Takes a snapshot of your character and produces a ranked "fix this first" list:

* **Enhancement upgrades** — the biggest lever. Asks PLAYNC what each item
  looks like at its cap and reports the real numbers, not estimates.
* **Manastone swaps** — finds stones carrying stats that do nothing for your
  goal and names a replacement of the same grade, with the exact gain.
* **Empty sockets** — unused manastone slots.
* **Sub-stat rerolls** — rolled lines wasted on defensive stats.
* **Arcana sets** — flags a lone set piece contributing no set bonus.
* **Skill targets** — effective level against your targets, counting Daevanion
  and Arcana bonuses, which the raw API does not do for you.
* **Attribute check** — flags Dexterity outweighing Precision.

### Goal profiles

The same gear gets ranked differently depending on what you are chasing:

| Profile | What it optimises |
|---|---|
| `combat_power` | Balanced offence and defence, for the headline CP number. Default. |
| `pve_dps` | Damage only. Defence scores zero. |
| `accuracy_rush` | Everything subordinated to the raid accuracy floor. |

```bash
aion2 profiles                    # list them
aion2 audit                       # uses the default
aion2 audit --profile pve_dps     # or pick one
```

On the dashboard they are buttons at the top of the page.

Everything is keyed off `config/thresholds.yaml`, which you can retune without
touching code — including adding your own profile.

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Find your character and get your config values:

```bash
aion2 discover Epitet --region eu
```

Paste the printed values into `.env`. **Keep `AION2_CHARACTER_ID` exactly as
printed**, including any trailing `%3D` — it is percent-encoded and decoding it
causes a 404.

Then:

```bash
aion2 snapshot     # ~60 rate-limited requests, about 4 minutes
aion2 audit        # ranked recommendations in the terminal
aion2 history      # how Combat Power has actually moved
aion2 serve        # dashboard on http://localhost:8080
```

The snapshot includes an enhancement scan, which costs one extra request per
item but means every later audit runs offline. Skip it with
`--no-enhancements` to halve the request count, at the cost of losing upgrade
recommendations.

To audit a raw capture directory with no network at all:

```bash
aion2 replay captures/epitet --all
```

---

## Docker

```bash
docker compose up -d --build
```

Local data lands in `./data`. Configuration is entirely env-driven, so the same
image runs anywhere.

For Unraid, layer the override on top:

```bash
docker compose -f docker-compose.yml -f deploy/unraid.override.yml up -d --build
```

Full walkthrough including the Cloudflare Tunnel in
[docs/deploy-unraid.md](docs/deploy-unraid.md). Short version: you do **not**
need a Cloudflare subdomain to run it, only to reach it from outside your
network.

---

## HTTP endpoints

| Route | Purpose |
|---|---|
| `GET /` | Dashboard |
| `GET /health` | Health check for Docker and Unraid |
| `GET /api/character` | Current snapshot summary |
| `GET /api/recommendations?profile=pve_dps` | Ranked recommendations as JSON |
| `GET /api/profiles` | Available goal profiles |
| `GET /api/history?stat=combat_power&days=90` | Time series |
| `POST /refresh` | Take a snapshot, respects the cooldown. `?force=true` overrides |

---

## Important: what the rankings are, and are not

**Combat Power cannot be calculated.** PLAYNC publishes `combatPower` as a
single number for your current state only. There is no endpoint that answers
"what would it be if", and the formula behind it is not published.

So the ranking is a **weighting heuristic**, tuned per goal profile in
`config/thresholds.yaml`. It never claims a CP delta.

The individual stat gains, however, are exact. The item endpoint accepts
`enchantLevel` as a free parameter and recalculates, so "Penetration +400 from
taking this rune to +10" is PLAYNC's own number, not ours.

The honest CP measurement is empirical. Snapshots record `combatPower` over
time, so once you act on a recommendation the next snapshot shows what it
actually did:

```bash
aion2 history combat_power
```

---

## Important: what Accuracy means here

**The API does not publish an aggregate Accuracy figure.** No total Accuracy,
no PvE Accuracy, no Block Penetration. This was confirmed by probing every
plausible endpoint; see `docs/endpoints.md` section 4.

So this project tracks Accuracy as **components**, never as a derived total.
The dashboard's "Gear Accuracy" is flat Accuracy readable from your gear and is
labelled as such. It is deliberately not a claim about your in-game number,
because the class base value by level and any active buffs are not published
and cannot be inferred.

For reference, questlog.gg shows the same kind of figure and arrives at roughly
the same number, so this is the ecosystem norm rather than a shortcoming here.

Record your real in-game totals in a manual file if you want absolute numbers
tracked alongside the component breakdown.

---

## Project layout

```
src/aion2/
  client.py      the only module that talks HTTP to PLAYNC
  models.py      parsing, including every optional-key trap
  stats.py       stat vocabulary and the manastone value table
  fetch.py       snapshot orchestration
  recommend.py   the recommendation engine
  db.py          SQLite snapshots and per-stat components
  web.py         FastAPI read-only dashboard
  replay.py      rebuild a character offline, from captures or the DB
config/
  thresholds.yaml  targets, weights and wasted-stat list
docs/
  endpoints.md     full endpoint reference and the evidence behind every rule
scripts/
  discover.ps1     capture every endpoint for a character
tests/
  fixtures/        BOM-free captures chosen to catch specific bugs
```

---

## Development notes

Run the tests:

```bash
pytest -q
```

The test suite exists mostly to lock in traps that actually bit us. A few worth
knowing before you change anything:

* **`characterId` is percent-encoded.** Build query strings by hand. Passing it
  through a urlencode helper double-encodes it and the API returns 404.
* **`AbnormalAccuracy` is not Accuracy.** It means Status Effect Chance. Match
  stat ids exactly, never with a substring test. Two ids, `WeaponAccuracy` and
  `Accuracy`, are both genuine and both must count.
* **Item stat arrays are absent, not empty.** Belt, Amulet and Runes carry only
  `mainStats`. Arcana add `subSkills` and `set`. Use a safe default everywhere.
* **Daevanion board ids differ per class** and are not contiguous. Enumerate
  them from the info response.
* **An item's displayed value is `value + extra`**, where `extra` is the
  enhancement bonus.
* **Effective skill level is three sources summed**, matched by name.
* **Stat display names are localized**, and `ItemLevel` returns untranslated
  Korean. Key on `type` or `id`.
* **The site serves its error page with HTTP 200.** Validate content type, not
  just status.

---

## Limits

Two things are genuinely not possible from the public API:

* **No item catalog exists for Global.** Twelve candidate endpoints all return
  404. So "swap to a different item" cannot be recommended, only changes to
  gear you already own.
* **Sub-stat ranges are not published.** The engine can flag that a stat is
  useless for your build, but not whether a given roll is good within its
  range.

Manastone values come from a table derived from gear observed so far, and are
approximate — a grade defines a band rather than a fixed number.

---

## Conduct

Public data only, no authentication, no game client interaction, and no bulk
scraping of other players. Requests are spaced by `AION2_RATE_LIMIT_SECONDS`,
which has a 2 second floor, and snapshots are subject to a cooldown.
