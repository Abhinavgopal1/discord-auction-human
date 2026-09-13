# Player pools and game prices

## 2026/27 roster

`players/26-27/` is a curated snapshot checked on **11 September 2026**. It contains **257 unique players from 17 clubs**, with all nine playable positions and all three price tiers at each position. It is a selected game pool, not a complete worldwide registration database.

| Position | Players |
| --- | ---: |
| GK | 32 |
| CB | 40 |
| LB | 29 |
| RB | 22 |
| CM | 38 |
| CAM | 31 |
| LW | 21 |
| RW | 22 |
| ST | 22 |

Each current-season row records `club`, `nation`, `league`, `season`, `rating`, `source_url`, and `verified_on`, alongside the existing `name`, `position`, `tier`, and `base_price` fields. A player occurs once in this season pool. Players who cover several roles receive one game position so the same player cannot appear in two simultaneous position pools.

### Official membership sources

Club membership was checked against these official sources, including the post-window Premier League lists. The per-player `source_url` identifies the relevant source.

- [Premier League: 2026/27 submitted squad lists, 3 September 2026](https://www.premierleague.com/en/news/4706139/see-all-the-202627-premier-league-squad-lists): Arsenal, Aston Villa, Bournemouth, Brentford, Brighton, Chelsea, Crystal Palace, Everton, Liverpool, Manchester City, Manchester United, Newcastle and Nottingham Forest. Selected under-21 players are included only where the list does not mark them out on loan.
- [Real Madrid: 2026/27 squad numbers, 10 August 2026](https://www.realmadrid.com/en-US/news/football/first-team/latest-news/dorsales-del-real-madrid-para-la-temporada-2026-27-10-08-2026), also checked against the [current first-team roster](https://www.realmadrid.com/en-US/football/first-team/players).
- [Barcelona: 2026/27 squad numbers confirmed, 2 September 2026](https://www.fcbarcelona.com/en/football/first-team/news/4570894/202627-first-team-jersey-numbers-confirmed).
- [Paris Saint-Germain: 2026/27 men's squad](https://www.psg.fr/en/mens-football/squad).
- [Bayern Munich: 2026/27 first-team squad](https://fcbayern.com/de/teams/profis).

**Verification boundaries:** These sources establish listed squad membership, not a guarantee of match availability, injury status, registration in every competition, or a future transfer. The snapshot is not automatically updated. Names use familiar display names rather than every legal given name. `nation` is a curated football nationality label; a separate national-team profile was not checked for every row. The nine exact positions are editorial game assignments because club lists often use only defender/midfielder/forward. Defensive midfielders use CM, wing-backs use LB/RB, and second strikers may use CAM. Ratings are subjective game-balance scores, not official EA, FIFA, UEFA or club ratings.

## Deterministic in-game prices

Current-season base prices are **fictional auction starting prices**, not transfer fees, salaries, or market valuations. The displayed dollar sign is the bot's existing game currency. Equal ratings always receive equal starting prices, regardless of position or random load order:

```text
r = clamp(rating, 65, 94)
price_in_millions = round(1 + 49 * ((r - 65) / 29) ** 2)
base_price = price_in_millions * 1,000,000
```

The result stays between $1 million and $50 million in whole-million steps. The curve leaves affordable squad options and progressively prices elite players higher; it does not charge a positional premium. Actual auction bids can exceed the starting price.

| Rating | Starting price | Tier |
| ---: | ---: | :--- |
| 70 | $2m | C |
| 80 | $14m | C |
| 86 | $27m | B |
| 90 | $37m | B |
| 91 | $40m | A |
| 94 | $50m | A |

Tier A is $40m–$50m, B is $25m–less than $40m, and C is less than $25m. `tier` is consistent with `base_price`; the stored rating supplies finer-grained player quality. Existing historical prices retain their original design, apart from the concrete corrections below.

## Historical data repairs

- Removed 47 repeated rows within individual position files. The first existing row is retained, preserving that file's original ordering and valuation. Cross-position appearances in historical sets remain intentional legacy alternatives.
- Raised five La Liga CAM starting prices below the configured minimum to $1m. This prevents the previous loader from replacing them with random prices.
- Corrected 18 stored tier labels to match their existing prices.
- Corrected Lionel Messi's position in `laliga/rw.json` to RW.
- Replaced the accidental copy of all 32 CAM rows in `24-25/lw.json` with 24 era-appropriate left-wing options. These use curated historical game prices, with no present-day club claims. The CAM pool remains intact. [UEFA's archived 2024/25 statistics](https://www.uefa.com/uefachampionsleague/history/seasons/2025/statistics/) and [2024/25 tactical analysis of PSG's wingers](https://www.uefa.com/uefachampionsleague/news/0299-1da9a3d4171f-925c0ba282a4-1000--champions-league-performance-insights-paris/) provide historical context; this replacement is an editorial game pool rather than an official position ranking.

The archival sets have not had every biography or historical role independently re-researched. Their original mixed-era intent remains. The bundled partial `seriea` and `bundesliga` archives are not complete nine-position season pools.

## Updating the next snapshot

1. Use a dated official post-window squad list or current club roster; confirm outgoing loans and remove departed players from the new season pool.
2. Update membership, source link and verification date together. Keep one record per player in the season across all positions.
3. Review the editorial rating, recalculate the documented price, and derive the tier from the price. Never present the result as an official valuation.
4. Run `python -m unittest discover -s tests -p test_rosters.py` to validate schema, uniqueness, positional coverage, provenance fields and price consistency.
