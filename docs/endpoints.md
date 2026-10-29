# AION 2 Global — Endpoint Discovery (Phase 0)

**Verified:** 2026-10-28
**Method:** direct scripted HTTPS GETs from Windows PowerShell (no browser, no cookies, no auth). Roughly 80 requests total.
**Sample characters:** three level-45 characters. Our own **Ranger `Epitet`**, region `eu`, serverId `1302` (Nezekan), plus two public characters used for cross-class comparison, a **Gladiator** on serverId `2314` and a **Chanter** on serverId `2323`. Comparing three classes is what exposed the class-specific Daevanion board ids, the optional item stat arrays, and the Arcana slots that only appear when equipped.

---

## 1. Headline findings

| Question from the build brief | Answer |
|---|---|
| Does the site block non-browser requests? | **No.** Plain scripted GETs with a realistic `User-Agent` work. No cookies, no session, no Cloudflare challenge, no `Origin` check. |
| Are the community-documented endpoints real for Global? | **Yes**, all five confirmed returning 200 + JSON. |
| Are manastone details public? | **Yes.** Per-item `magicStoneStat` includes stat id, value, grade and slot position. Brief guessed these were probably unavailable. |
| Are god stones / Theostones public? | **Yes.** Per-item `godStoneStat` with name, full effect text and grade. |
| Is Arcana data public? | **Yes.** Equipped Arcana occupy slots 41–45 of the normal equipment list, with full detail including set membership and the skill levels they grant. The brief guessed these were probably unavailable. |
| Is Genus Insight public? | **No.** Confirmed absent — the strings `Genus`, `Insight` and `Cogni` appear nowhere in a full 34-file capture. Needs manual entry. |
| Are Stigma skills identifiable? | **Yes**, via `category: "Dp"` in the skill list. |
| Is total/aggregate Accuracy public? | **No.** This is the significant finding — see §4. |
| Rate limiting / blocking observed? | None. ~50 requests at 3–4 s spacing, zero 429s, zero blocks. |

---

## 2. Confirmed endpoints

All responses are `application/json`. Shared query parameters are `region`, `lang=en-US`, `characterId`, `serverId`.

### 2.1 Character search

```
GET https://api-search.plaync.com/aion2global/search/v2/character
    ?keyword=<name>&region=<region>&localeInfo=en-US&page=1&size=20
    [&serverId=<serverId>]
```

Returns `{ list: [...], pagination: { page, size, total, endPage } }`. Each entry:

```json
{
  "characterId": "RGAnjCGnjSBTdAp8DT6Tf0KB9AYZUUxJQj7QhhCv4iM%3D",
  "name": "<strong>Kira</strong>",
  "race": 2, "pcId": 8, "level": 45,
  "serverId": 2314, "serverName": "Odar",
  "profileImageUrl": "/game_profile_images/aion2global/images?gameServerKey=2314&charKey=651333096108622016",
  "region": "eu"
}
```

Gotchas:
- `name` is wrapped in `<strong>` highlight tags around the matched substring. **Must be stripped.**
- Matching is case-insensitive substring, not exact. `keyword=Kira` also returns `Akira`.
- `characterId` arrives **already percent-encoded** (`%3D` = `=`). Pass it through verbatim; do not re-encode or you get a 404.
- `serverId` must belong to the given `region` or you get an empty list, not an error.
- An unmatched keyword returns `200` with `list: []`. Empty is not an error.

### 2.2 Character info

```
GET https://aion2.plaync.com/api/character/info?region=&lang=en-US&characterId=&serverId=
```

Top-level keys: `profile`, `stat`, `title`, `ranking`, `daevanion`.

- `profile` — `characterName`, `characterLevel`, `className`, `combatPower`, `raceName`, `serverName`, `titleName`, `titleGrade`, `pcId`, `profileImage`. Here `characterId` is returned **decoded** (`=` not `%3D`).
- `stat.statList` — 6 base stats + 10 god stats + `ItemLevel`. See §3.
- `title` — `ownedCount`, `totalCount`, and the 3 stat-bearing equipped titles with `statList` / `equipStatList`.

  **Titles are two independent systems.** `profile.titleName` / `titleId` / `titleGrade` is the single **cosmetic displayed** title, and is blank with `titleId: 0` when none is shown. `title.titleList` is always the **3 stat-bearing equipped** titles, one per `equipCategory` of `Attack`, `Defense` and `Etc`. Verified on two characters: our Ranger has no cosmetic title but all three stat titles equipped. Read `titleList` for stats and ignore `titleName` entirely.
- `ranking.rankingList` — **always `null`**. Public ranking boards were removed in 2026.
- `daevanion.boardList` — per-board `id`, `name`, `open`, `openNodeCount`, `totalNodeCount`, `openPercent`.

**Daevanion board ids are class-specific — never hardcode them.** Verified against two classes:

| Class | Board ids | Board names |
|---|---|---|
| Gladiator | 11, 12, 13, 14, 15, 16 | Nezekan, Zikel, Vaizel, Triniel, Ariel, Azphel |
| **Ranger** | **31, 32, 33, 34, 35, 36** | Nezekan, Zikel, Vaizel, Triniel, Ariel, Azphel |
| Chanter | 81, 82, 83, 84, 86 | Nezekan, Zikel, Vaizel, Triniel, Azphel |

The god names repeat but the numeric ids differ by class. The pattern looks like a class index in the leading digit, but do not rely on it — always enumerate `boardList` from the info response and use the ids it returns. Note the Chanter had no board 85 at all, so the id set is not contiguous either.

Also `open: 1` means **the board is unlocked**, not that it has progress. The Chanter's board 86 had `open: 1` with `openNodeCount: 0`.

### 2.3 Character equipment

```
GET https://aion2.plaync.com/api/character/equipment?region=&lang=en-US&characterId=&serverId=
```

Top-level keys: `equipment` (`equipmentList`, `skinList`), `petwing` (`pet`, `wing`, `wingSkin`), `skill` (`skillList`).

`equipmentList` is a **summary only** — no stat lines. Per entry: `id`, `name`, `grade`, `enchantLevel`, `exceedLevel`, `slotPos`, `slotPosName`, `icon`. To get stats you must call the item endpoint once per slot (§2.4).

Slot map, up to 25 slots:

| slotPos | Name | slotPos | Name | slotPos | Name |
|---|---|---|---|---|---|
| 1 | MainHand | 11 | Earring1 | 19 | Cape |
| 2 | SubHand | 12 | Earring2 | 22 | Amulet |
| 3 | Helmet | 13 | Ring1 | 23 | Rune1 |
| 4 | Shoulder | 14 | Ring2 | 24 | Rune2 |
| 5 | Torso | 15 | Bracelet1 | **41** | **Arcana1** |
| 6 | Pants | 16 | Bracelet2 | **42** | **Arcana2** |
| 7 | Gloves | 17 | Belt | **43** | **Arcana3** |
| 8 | Boots | | | **44** | **Arcana4** |
| 10 | Necklace | | | **45** | **Arcana5** |

**Slots 41–45 are Arcana** and appear only when Arcana are equipped. The Gladiator and Chanter samples returned 20 slots and no 41–45, which is why Arcana was initially and wrongly recorded as unavailable. Our Ranger returned 25.

Gaps 9, 18, 20, 21 and 25–40 were never present. Do not assume a contiguous range; iterate what the list returns.

`skillList` entries carry `name`, `skillLevel`, `category`, `acquired`, `equip`, `needLevel`, `id`.

**`category` vocabulary**, from 35 Ranger skills: `Active` (12), `Passive` (10), `Dp` (13).

**`Dp` is the Stigma category.** The brief's stigmas — Vaizel's Authority, Bow of Blessing, Griffon Arrow, Supporting Fire — all came back as `Dp`. Note that `Dp` skills receive **no** Daevanion or Arcana bonuses; their `skillLevel` is final. All other categories do receive bonuses, see §2.4.1.

### 2.4 Item detail — the richest endpoint

```
GET https://aion2.plaync.com/api/character/equipment/item
    ?region=&lang=en-US&characterId=&serverId=
    &id=<itemId>&enchantLevel=<n>&slotPos=<n>
```

All three of `id`, `enchantLevel`, `slotPos` are required and must match the equipped item from §2.3.

Returns, per item: `name`, `grade`, `gradeName`, `level`, `enchantLevel`, `maxEnchantLevel`, `categoryName`, `classNames`, `magicStoneSlotCount`, `godStoneSlotCount`, `soulBindRate`, `sources`, plus four stat arrays:

| Array | Meaning | Audit relevance |
|---|---|---|
| `mainStats` | Fixed base lines. `{id, name, value, minValue, extra, exceed}` | Weapon `WeaponAccuracy` lives here |
| `subStats` | Rolled / rerollable lines | **Primary reroll-candidate source** |
| `magicStoneStat` | Manastones. `{id, name, value, grade, slotPos}` | **Primary reroll-candidate source** |
| `godStoneStat` | Theostone. `{name, desc, grade, slotPos}` | Reported, not audited |

Arcana slots add **two more arrays**, bringing the total to six:

| Array | Meaning | Audit relevance |
|---|---|---|
| `subSkills` | Skill levels this item grants. `{id, name, level, icon}` | **Required for effective skill level** |
| `set` | Set membership. `{id, name, equippedCount, items[]}` | Confirms set bonuses, e.g. Primal Vigor |

**Every array is optional and omitted entirely rather than returned empty.** Verified across 25 Ranger slots and 20 slots on each of two other characters:

| Slot type | mainStats | subStats | magicStoneStat | godStoneStat | subSkills | set |
|---|---|---|---|---|---|---|
| Weapons, armour, accessories | yes | yes | yes | only if engraved | no | no |
| Belt, Amulet, Rune1, Rune2 | yes | **no** | **no** | no | no | no |
| **Arcana (41–45)** | yes | no | no | no | **yes** | **yes** |

So Belt, Amulet and Runes carry `mainStats` only, and Arcana carry `mainStats` plus `subSkills` plus `set`. The parser must use a safe default for every array, e.g. `item.get("magicStoneStat", [])`. A direct index will raise on roughly a third of slots.

### 2.4.1 Effective skill level comes from three sources

A skill's real level is **not** the `skillLevel` from the equipment endpoint. It is the sum of three independent sources:

```
effective level = skillList.skillLevel
                + sum of Daevanion openSkillEffectList matches across all boards
                + sum of Arcana subSkills matches across slots 41-45
```

Worked example from our Ranger, where ignoring the extra sources would under-report by 10 levels:

| Skill | Base | Daevanion | Arcana | Effective |
|---|---|---|---|---|
| Deadshot | 20 | 4 | 6 | **30** |
| Snipe | 17 | 4 | 2 | **23** |
| Gale Arrow | 16 | 4 | 2 | **22** |
| Vaizel's Authority (`Dp`) | 20 | 0 | 0 | **20** |

Matching is by **skill name string**, since Daevanion and Arcana effects do not share the skill ids from `skillList`. Daevanion gives free text like `"Keen Strike +1"`; Arcana gives a structured `{name, level}`.

Worked example — the first sample character's main hand carried four manastones: MP, Evasion, Block, Critical Hit Resist. By the brief's §5 rules **all four are wasted-DPS lines**. This confirms `audit_build` is fully implementable from public data.

### 2.5 Daevanion board detail

```
GET https://aion2.plaync.com/api/character/daevanion/detail?region=&lang=en-US&characterId=&serverId=&boardId=<id>
```

Returns three top-level keys (~41 KB for one board): `nodeList`, `openStatEffectList`, `openSkillEffectList`.

**`openStatEffectList` and `openSkillEffectList` are pre-aggregated board totals** for already-opened nodes. This is a significant convenience — the per-board Daevanion contribution does **not** need to be summed by hand from `nodeList`:

```json
"openStatEffectList":  [{"desc": "HP +600"}, {"desc": "Critical Hit +30"},
                        {"desc": "Attack Bonus +27"}, {"desc": "Cooldown Reduction +3%"}, ...]
"openSkillEffectList": [{"desc": "Keen Strike +1"}, {"desc": "Survival Stance +2"}, ...]
```

`openSkillEffectList` matters for the §5 skill-target checks: a skill's **effective level is its `skillLevel` from the equipment endpoint plus any Daevanion bonus here**. Checking `skillLevel` alone will under-report.

`nodeList` is the full board grid. Per node: `boardId`, `nodeId`, `row`, `col`, `grade`, `type`, `open` (0/1), `name`, `icon`, `effectList[].desc`.

Node `type` vocabulary, from board 11 (225 grid cells):

| type | Count | Meaning |
|---|---|---|
| `None` | 136 | Empty grid padding, blank name, empty `effectList` — filter out |
| `Stat` | 66 | Stat bonus, e.g. `"Critical Hit Resist +5"`, `"Defense Bonus +30"` |
| `SkillLevel` | 22 | Skill level bonus, e.g. `"Keen Strike +1"` |
| `Start` | 1 | Board entry point |

Consistency check that confirms the model: `info.daevanion.boardList` reported `totalNodeCount: 88` for board 11, and `Stat` + `SkillLevel` = 66 + 22 = 88. So `totalNodeCount` counts **real nodes only**, excluding padding and the start node. `openNodeCount: 56` matched the count of `open == 1` exactly. This held across all five boards of a second character.

Board shape varies while the grid does not. Measured across five boards:

| Board | Grid cells | Real nodes | Opened | Stat effects | Skill effects |
|---|---|---|---|---|---|
| 81 Nezekan | 225 | 88 | 47 | 8 | 10 |
| 82 Zikel | 225 | 88 | 50 | 8 | 13 |
| 83 Vaizel | 225 | 88 | 36 | 7 | 10 |
| 84 Triniel | 225 | 116 | 43 | 7 | 11 |
| 86 Azphel | 225 | 152 | 0 | 0 | 0 |

- The grid is always 225 cells; real node count varies (88, 116, 152), padding absorbs the difference.
- **A board with zero opened nodes returns empty arrays, not null.** Board 86 gave `[]` for both aggregates — safe to iterate without a guard.
- Some boards have **no `SkillLevel` nodes at all**. Board 86 was 152 `Stat` and 0 `SkillLevel`.
- `open: 1` means unlocked. Only opened nodes contribute.
- Effects are **free-text descriptions**, not structured id/value pairs. Parsing requires a regex over `desc` — note both flat (`+30`) and percentage (`+3%`) forms appear.

### 2.6 Game data (no character required)

```
GET https://aion2.plaync.com/en-us/api/gameinfo/pcdata?lang=en
GET https://aion2.plaync.com/en-us/api/gameinfo/servers?lang=en&region=<region>
```

- `pcdata` → `pcDataList[]` of `{id, className, classText, genderName, raceName}`. This maps the `pcId` from search/profile to a class and gender.
- `servers` → `serverList[]` of `{raceId, serverId, serverName, serverShortName}`. 46 entries per region.

**Path inconsistency to respect:** character endpoints are at `/api/character/...` with no locale prefix. Game data endpoints are at `/en-us/api/gameinfo/...` **with** the prefix. Using the wrong form gives a 404. Both were tested.

---

## 3. Stat vocabulary

### 3.1 `info.stat.statList` — keyed by `type`

| type | Display name | Notes |
|---|---|---|
| `STR` | Might | |
| `DEX` | Dexterity | The brief's "Dexterity 35" |
| `INT` | Intelligence | |
| `CON` | Constitution | |
| `AGI` | **Precision** | The brief's "Precision 7" — note it is `AGI`, not `PRE` |
| `WIS` | Willpower | |
| `Justice` | Justice [Nezekan] | Defense + Perfect Chance |
| `Freedom` | Freedom [Vaizel] | **Accuracy + Evasion** |
| `Illusion` | Illusion [Kaisinel] | Cooldown + Endurance Penetration |
| `Life` | Life [Yustiel] | HP + Regeneration |
| `Time` | Time [Siel] | Combat Speed + Double Resist |
| `Destruction` | Destruction [Zikel] | Attack + Perfect Resist |
| `Death` | Death [Triniel] | **Critical Hit** + Regeneration Penetration |
| `Wisdom` | Wisdom [Lumiel] | MP Cost + Double Chance |
| `Destiny` | Destiny [Marchutan] | MP + Endurance |
| `Space` | Space [Israphel] | Move Speed + Block |
| `ItemLevel` | *(Korean text)* | **Localization bug** — `name` returns `아이템레벨` even with `lang=en-US`. Key on `type`, never on `name`. |

God stats carry a `statSecondList` of derived percentage effects, e.g. Freedom 24 → `["Accuracy increase +2.4%", "Evasion increase +2.4%"]`. Ratio is value ÷ 10 = percent.

### 3.2 Item stat ids

38 distinct ids observed on our Ranger's 25 slots:

| id | Display name | id | Display name |
|---|---|---|---|
| `WeaponFixingDamage` | Attack | `AmplifyAllDamage` | Damage Boost |
| `WeaponAccuracy` | Accuracy | `AmplifyWeaponDamage` | Weapon Damage Boost |
| `Accuracy` | Accuracy Bonus | `DamageRatio` | Attack increase |
| `AbnormalAccuracy` | **Status Effect Chance** | `DefensePierce` | Penetration |
| `Critical` | Critical Hit | `AdditionalHitRate` | Multi-hit Chance |
| `CriticalResist` | Critical Hit Resist | `Perfect` | Perfect Chance |
| `CriticalDamageDefense` | Critical Damage Defense | `PvEAmplifyDamage` | PvE Damage Boost |
| `Block` | Block | `PvEDecreaseDamage` | PvE Damage Tolerance |
| `Evasion` | Evasion Bonus | `PvPDamageDefense` | PvP Defense |
| `ArmorEvasion` | Evasion | `SealStoneAddDamage` | Power Shard Damage Bonus |
| `ArmorDefense` | Defense | `FrontAttackDefense` | Front Defense |
| `HPMax` / `MPMax` | HP / MP | `CombatSpeed` | Combat Speed |
| `HPRegen` / `MPRegen` | Natural HP / MP Regen | `MoveSpeed` | Move Speed |
| `Restoration` | Regeneration | `HpPotionRate` | HP Potion Recovery increase |
| `STR` `DEX` `CON` | Might, Dexterity, Constitution | `Justice` `Death` `Time` `Life` `Illusion` | god stats, also appear on Arcana |

**Trap — `AbnormalAccuracy` is not Accuracy.** It displays as "Status Effect Chance" and is measured in percent. Any substring match on `Accuracy` will pick it up and silently inflate the Accuracy total. On our Ranger it added a phantom 18 points. **Match stat ids exactly**, never by substring.

Conversely there are genuinely **two** flat accuracy ids that both count: `WeaponAccuracy` ("Accuracy") and `Accuracy` ("Accuracy Bonus"). Matching only one under-reports.

**Treat the id set as open.** Three classes produced three different id sets. Pass unknown ids through rather than dropping them.

Mapping to the brief's §5 wasted-DPS rule, the reroll-candidate ids are `DEX`, `Evasion`, `ArmorEvasion`, `Block`, `ArmorDefense` and `CriticalResist`. Everything else passes.

---

## 4. The Accuracy problem — important

**The API does not expose aggregate combat stats.** There is no total Accuracy, PvE Accuracy, Critical Hit total, or Block Penetration anywhere in the public data.

Verified by probing 23 candidate paths under `/api/character/` — every one returned 404 except the four documented in §2:

```
ability combat status statistics profile summary arcana genus collection
insight card stigma skill title rune amulet pet wing info/stat
equipment/stat stat/detail substats abilities      → all 404
```

`info.stat.statList` contains only base stats, god stats and item level. The brief's primary goal — tracking "Accuracy ~757 vs ~1,100–1,200 target" — cannot be read directly.

**What is available instead:** every individual contribution. Measured on our Ranger, whose in-game Accuracy the brief records as roughly 757:

| Accuracy source | Readable value |
|---|---|
| Bow `WeaponAccuracy` (main) | 100 |
| SubHand `WeaponAccuracy` (main) | 50 |
| Helmet, Torso, Earring1 `WeaponAccuracy` (sub) | 137 |
| Manastone `Accuracy` lines on SubHand, Earring1, Cape | 24 |
| **Total flat Accuracy readable from gear** | **311** |
| Daevanion boards | PvP Accuracy +10 only, no PvE |
| Titles | PvP Accuracy +80 only, no PvE |
| Precision (`AGI` 21) | +2.1%, a multiplier not a flat value |
| Freedom god stat (38) | +3.8%, a multiplier not a flat value |

So 311 flat plus about 6% in multipliers against a reported in-game figure near 757. The gap is the **class base value by level**, which is not published anywhere, plus any food or scroll **buffs** active when the figure was read. That gap is why a derived total cannot be trusted as an absolute number.

Note also that the only Accuracy the Daevanion boards and titles contribute is **PvP** Accuracy, which does nothing for the brief's PvE raid target.

**This forces a design decision for Phase 1** — three options:

1. **Derive it.** Sum the readable contributions into an estimated total. More feasible than first assumed, since Daevanion arrives pre-aggregated, but still incomplete — base-by-level and buffs are missing, so the figure will not match the client. Risk: a number that is confidently wrong.
2. **Track components, not the total.** Report Accuracy contribution per item/stone/node and trend each over time. Never shows "757", but answers the actionable question — *which lines are wasted and what should I reroll* — completely and accurately.
3. **Manual entry for totals, API for components.** Type the in-game totals into the manual-data file occasionally. Accurate absolute numbers, combined with automatic per-item detail.

Recommendation: **2 + 3.** Option 2 delivers the full `audit_build` value with zero guesswork, and 3 covers the headline number without pretending to compute it. Option 1 alone would produce a plausible-looking Accuracy figure that silently disagrees with the game client.

The manual-data file planned in the brief (§2, `get_manual_data`) therefore needs to hold only **Genus Insight** and the **in-game aggregate stat totals**. Arcana does not belong there after all, since §2.3 shows it is fully readable from the API.

---

## 5. Region and server codes

`region` values: `nae`, `naw`, `eu`, `la`, `as`.

**`serverId` encodes both region and race** — confirmed by comparing server lists across regions:

| Region | Race 1 (Light / Elyos) | Race 2 (Dark / Asmodian) |
|---|---|---|
| `nae` | 1101–1123 | 2101–2123 |
| `naw` | 1201–1223 | 2201–2223 |
| `eu` | 1301–1323 | 2301–2323 |
| `la` | 1401–1423 | 2401–2423 |
| `as` | 1501–1523 | 2501–2523 |

Server names are identical across regions — "Siel" is 1101 on `nae` and 1301 on `eu`. So a server name alone is ambiguous; always store `region` + `serverId` together.

The `servers` endpoint defaults to `nae` when `region` is omitted.

---

## 6. Fetcher requirements derived from this

1. **Pass `characterId` through verbatim.** It arrives percent-encoded from search. Re-encoding breaks it. Build query strings manually rather than through a helper that escapes values — this is the single easiest way to break the client.
2. **Strip `<strong>` tags** from search result names.
3. **Key stats on `id` / `type`, never on `name`.** Names are localized and at least one (`ItemLevel`) is an untranslated Korean string.
4. **An HTML error page returns HTTP 200.** The Korean "page not found" page comes back as 200 with a `page-error` body class. API endpoints do return real 404s, but any non-API request must be validated on content, not status.
5. **One item request per equipped slot** — 20 calls for a full gear read. At the brief's 3–5 s rate limit that is 60–100 s per snapshot. Budget for it, and cache aggressively since gear changes rarely.
6. **Empty list ≠ error** for search.
7. **Daevanion effects are free text.** Parse `desc` with a regex handling both `+30` and `+3%` forms; keep the raw string alongside the parsed value so a format change is visible rather than silent.
8. **Prefer `openStatEffectList` over summing `nodeList`** for board totals, and filter `type: "None"` nodes whenever walking the grid.
9. **Add `openSkillEffectList` to `skillLevel`** to get a skill's effective level. Reading `skillLevel` alone under-reports.
10. `ranking.rankingList` is always `null` — do not build on it.
11. **Expect unknown stat ids.** Pass them through; never silently drop.
12. **Search results are not stably ordered.** Two identical queries in one session returned different sets and different counts. Do not rely on position. Match on exact name plus `serverId`, and prefer the highest level when a name is ambiguous.
13. **Treat all six item stat arrays as optional** with a safe default. Belt, Amulet and Runes omit `subStats` and `magicStoneStat`; Arcana omit both and add `subSkills` and `set` instead.
14. **Never hardcode Daevanion board ids** — they differ per class and are not contiguous. Enumerate `info.daevanion.boardList`.
15. **Match stat ids exactly, never by substring.** `AbnormalAccuracy` means Status Effect Chance and will corrupt an Accuracy total. Conversely both `WeaponAccuracy` and `Accuracy` are real accuracy and both must be counted.
16. **Compute effective skill level from three sources**, not from `skillLevel` alone. See §2.4.1. Match on skill name, since ids are not shared across sources.
17. **Equipment slot count is class- and gear-dependent.** Expect 20 to 25 slots. Arcana only appear when equipped, so an Arcana-aware parser must not assume they exist.
18. **Write fixtures without a BOM.** PowerShell's `Out-File -Encoding utf8` adds one and standard JSON parsers reject it.

A full snapshot costs 28–34 requests: 1 info + 1 equipment + 20–25 items + 5–6 Daevanion boards. Our Ranger's full capture was 34 files. Measured at 2 s spacing a 28-file capture took about 70 s; at the brief's 3–5 s rate limit budget 2–3 minutes per character.

---

## 7. Re-running discovery

`scripts/discover.ps1` automates everything above. It resolves a character by name, then captures info, equipment, one item detail per equipped slot and every opened Daevanion board, writing BOM-free JSON:

```powershell
.\scripts\discover.ps1 -Name <CharacterName> -Region eu
```

It prints the resolved `AION2_REGION` / `AION2_SERVER_ID` / `AION2_CHARACTER_ID` ready to paste into `.env`, strips the `<strong>` tags, picks the exact-name highest-level match, and defaults to 4 s between requests. Verified end to end: 28 files captured in one run.

---

## 7a. Presets — a hard limit

AION 2 has presets for gear, skills and pets. **The API exposes only the
active one**, with no way to request another and no field saying which is
live.

Verified by probing the equipment endpoint with thirteen candidate selectors
(`preset`, `presetId`, `presetNo`, `presetIndex`, `slotIndex`, `setId`,
`setNo`, `loadout`, `buildId`, `tab`, `page`, `index`, `type`, `equipType`),
each at two values. Every single response was **byte-identical** to the
baseline, same MD5 and same 16,034 bytes. The parameters are ignored.

`petwing.pet` likewise returns one pet, not a list.

Consequences the project has to live with:

* A snapshot captures whatever was equipped at that moment. Switching presets
  between snapshots looks like a sudden gain or loss in the history.
* Comparing two characters compares whichever preset each had active, which
  may not be the one either of them raids in.
* Recommendations target the active preset. Acting on them after switching
  preset applies them to the wrong build.

Handled by `compare.detect_preset_change`, which flags any snapshot pair
where five or more slots changed item at once, since that is far more likely
a preset switch than progression. The audit, the compare output and the
dashboard footer all state the limitation rather than letting it mislead.

---

## 8. Open items

**Resolved since the first pass:** the Ranger is captured, Arcana turned out to be public, Stigmas are identifiable via `category: "Dp"`, and Genus Insight is confirmed absent.

**Still open:**

- **Decide the Accuracy approach from §4** before writing the storage schema, since it determines whether snapshots store a single derived total or per-source rows. This is the one blocking decision.
- `exceedLevel` was 0 on every item across all three characters. Semantics unconfirmed — likely an endgame upgrade track not yet reachable at level 45.
- `subStatRandom` is `true` on Arcana and `false` on gear, and `subStatCount` was 0 everywhere while `subSkillCountMax` was 4 on Arcana. The relationship between these three fields is not yet understood; they probably describe reroll mechanics.
- `soulBindRate` was `"100"` on every item. Whether it ever differs, and whether the brief's "soul binding lines" are a separate concept, is unconfirmed.
- The brief's §5 asks for **Block Penetration**. No such stat id appeared. The closest is `DefensePierce` ("Penetration"). Needs confirmation that these are the same thing.
- Whether `Perfect` ("Perfect Chance") and the brief's accuracy target interact. It appears on Ranger gear but the brief never mentions it.

---

## 9. Fixtures

Saved under `tests/fixtures/` from this session:

Saved under `tests/fixtures/`, all BOM-free and confirmed to parse:

| File | Endpoint | Why it is useful |
|---|---|---|
| `01-pcdata.json` | gameinfo/pcdata | `pcId` → class/gender mapping |
| `02-servers-eu.json` | gameinfo/servers, region=eu | 13xx/23xx id range |
| `02-servers-nae.json` | gameinfo/servers, region=nae | 11xx/21xx range, proves region shifts ids |
| `03-search-results.json` | search, 5 matches | **Tests `<strong>` tag stripping** |
| `04-search-empty.json` | search, no match | Tests empty-is-not-an-error handling |
| `10-info.json` | character/info | Stat list incl. the Korean `ItemLevel` label |
| `12-equipment.json` | character/equipment | 20 slots, 35 skills, pet/wing |
| `20-item-mainhand.json` | equipment/item, weapon | 4 manastones + Theostone + main/sub stats |
| `21-item-necklace.json` | equipment/item, accessory | Accessory shape, no `godStoneStat` key at all |
| `22-daevanion-11.json` | daevanion/detail, board 11 | 225 nodes, all 4 node types, pre-aggregated totals |
| `23-item-belt-mainstats-only.json` | equipment/item, belt | **`mainStats` only** — the shape that crashes a naive parser |
| `30-ranger-info.json` | character/info, our Ranger | Board ids 31–36, blank cosmetic title, full god stats |
| `31-ranger-equipment-25slots.json` | character/equipment, our Ranger | **25 slots incl. Arcana**, 35 skills across all 3 categories |
| `32-item-arcana-subskills-set.json` | equipment/item, Arcana slot 41 | **`subSkills` + `set`** — shape nothing else covers |
| `33-item-bow.json` | equipment/item, bow +15 | Ranger weapon, `AbnormalAccuracy` trap present |
| `34-daevanion-ranger-36.json` | daevanion/detail, board 36 | 152 `Stat` nodes, no `SkillLevel` nodes |

Fixtures chosen to catch specific bugs:

| Bug it catches | Fixture |
|---|---|
| Missing `godStoneStat` key | `21-item-necklace.json` |
| Missing `subStats` and `magicStoneStat` | `23-item-belt-mainstats-only.json` |
| Arcana `subSkills` ignored, skill levels under-reported | `32-item-arcana-subskills-set.json` |
| `AbnormalAccuracy` counted as Accuracy | `33-item-bow.json` |
| `<strong>` tags left in names | `03-search-results.json` |
| Empty search treated as an error | `04-search-empty.json` |
| Hardcoded board ids | `30-ranger-info.json` vs `10-info.json` |

Keys are **absent**, not empty arrays. The Gladiator and Chanter data is public data for characters that are not ours, which makes it safe to commit; the Ranger fixtures are our own public profile data.

The raw 34-file Ranger capture lives in `captures/epitet/` and is kept out of the fixture set to keep tests fast.
