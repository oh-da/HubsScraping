# Hubs project vs Minhal

Two maps that put the Hubs project (HubsPrioritizing) next to Minhal (מנהל התכנון, `MT_Network.gpkg`):

| map | question | table |
|---|---|---|
| [`lines_map.html`](lines_map.html) | Which lines does each project use, and where do they differ? | `lines_comparison.xlsx` |
| [`hubs_map.html`](hubs_map.html) | Which hubs did each project find? | `hubs_comparison.xlsx` |

Served by GitHub Pages at https://oh-da.github.io/HubsScraping/HubsVsMinhal/.

## Inputs

From the project's Drive folder (HubsData, MinhalData), as of 2026-11-10. The first two were
already in the repository's `Data/` folder and are read from there; the rest are in `data/` here.

| file | used for |
|---|---|
| `../Data/All_nodes+lines_V1.1.csv` | Hubs project stops per line (`LINE_ID`, node, X/Y in EPSG:2039) |
| `../Data/MT_Network.gpkg` | Minhal `lines` (alignments, mode, years) and `stations` (with `hub_id`) |
| `data/Lines_and_Planned_Mode_V1.1.csv` | mode, name and area of each Hubs project line |
| `data/Routes_and_Nodes_V1.1.xlsx` | stop order along a line, where it is usable (see below) |
| `data/hub_prioritization_results.csv` | the 165 scored hub groups: tier, score, ranks, location |
| `data/hub_scoring_calculation.xlsx` | all 1,059 candidate groups and the eligibility rules |

## How these relate to the older comparison maps

`tod_scraper/compare_lines.py` and `compare_hubs.py` (pages `compare_lines.html`,
`compare_lines_overlay.html`, `compare.html`) compare the same two sources by geometry alone and
take the mode of a Hubs project line from the Minhal lines under it. The maps here use the Hubs
project's own mode table (`Lines_and_Planned_Mode`) and its final results and scoring workbook,
so they can show where the two projects put a *different mode* on the same corridor, and each
hub's tier, score and rank. The lines method here is simpler (distance classes, no routing along
the Minhal network and no offset correction; the 200 m tolerance absorbs the 40-80 m offset
between the sources), so its numbers are not interchangeable with `line_comparison.xlsx`.

## Rebuild

```bash
pip install -r requirements.txt
python build_lines_map.py     # lines_map.html, lines_comparison.xlsx  (about a minute)
python build_hubs_map.py      # hubs_map.html, hubs_comparison.xlsx
```

`common.py` holds the loaders and the mode mapping, `templates/` the two pages and their shared
stylesheet, `vendor/` Leaflet 1.9.4.

## Mode vocabulary

| map label | Hubs `Mode_Planned` | Minhal `type` |
|---|---|---|
| Metro | Metro | Metro |
| LRT | LRT | LRT |
| BRT | BRT | BRT |
| Rail | Suburban Rail, Interurban Rail | Rail |
| Rail fast | HighSpeed Rail | Rail fast |
| Funicular / cable | Funicular, Cable Line | FUNI |

When asking "does the other project have the same mode here", Rail and Rail fast are one family.

## Lines map: method

* **Hubs geometry.** The DataPack has stops, not alignments. Each line is drawn as straight links
  between consecutive stops. The stop order is taken from `Routes_and_Nodes` (sheet `All`) when
  that order is no more than 1.3 times the shortest path through the stops; for many lines the
  sheet is not in travel order (rail above all), and 41 lines are missing from it. In those cases
  the order is the shortest open path through the stops (nearest neighbour + 2-opt). Column
  `order_source` in the workbook says which was used: 125 lines from the sheet, 104 inferred. On
  the 125 lines where the sheet order is usable, the inferred order reproduces it exactly for 104.
* **Each line is one direction.** Hubs has 229 `LINE_ID`s, usually two per line. Hubs kilometres
  therefore count both directions; Minhal alignments are counted once. Compare shares, not km.
  Two `LINE_ID`s (R326-1, R326-2) have a single stop and cannot be drawn.
* **Classes.** Every stretch is `same` (the other project has a line of the same mode family
  within 200 m), `other` (a line of another mode family) or `none`. A Hubs link takes the worse of
  its two end stops, measured to Minhal's alignments. Minhal alignments are sampled every 50 m and
  measured to the Hubs links; runs shorter than 200 m are merged into their neighbour.
* **Heavy rail on the Minhal side is compared by station.** Hubs rail services skip stations, so
  their links say nothing about the track in between. Minhal rail alignments are drawn in grey,
  unclassed, and each Minhal rail station is matched to the nearest Hubs rail stop (500 m).
* **Clicking a line** lists every line of both projects within 200 m of the clicked point, with
  its distance. The list ignores the mode and class filters (a line they hide is marked), because
  the colour of a stretch is decided without them: a dashed "different mode" stretch is explained
  by a line of another mode, often on a parallel street.
* **Cancelled lines.** As elsewhere in this repository, Minhal lines with a `year_cancel` (20
  pieces) are left out of the Minhal network. Here they are a separate layer, off by default, and
  Hubs project lines are not compared against them.

## Hubs map: method

* **Hubs project.** The 165 scored groups of `hub_prioritization_results.csv`. The 138 with a tier
  (ארצי, מטרופוליני, עירוני) are compared; the 27 with `HubType = Not Hub` are an optional layer.
* **Minhal.** Stations sharing a `hub_id` form a hub (426; stops with a `year_cancel` are left
  out, none of them is in a hub). The 207 with two or more modes are compared; the 219
  single-mode hubs are an optional layer.
* **Matching** is by location only: two hubs are the same place when any stop of the Hubs group
  is within 400 m of any station of the Minhal hub. One hub can pair with several. The popup and
  the `both` sheet say whether the mode sets agree.
* **Popups** list everything the other project has within 400 m, nearest first, also what is
  left out of the comparison (single-mode Minhal hubs, "Not Hub" groups) and what the filters
  hide (marked). Only tiered hubs and multi-mode Minhal hubs decide the three sets.
* **Candidates.** `hub_scoring_calculation.xlsx` lists all 1,059 groups but has no coordinates and
  no node lists, so the 894 groups that failed eligibility cannot be drawn as groups. The map
  shows their stops instead (every stop that is in no scored group), and the eligibility rules are
  re-run in Python and written to the `eligibility` sheet. The script checks that the re-run
  reproduces the 165 eligible groups and their tiers exactly, and stops if it does not.

## Options

Both scripts take `--tolerance`; `build_lines_map.py` also `--rail-tolerance`,
`--step`, `--route-slack`; `build_hubs_map.py` also `--min-modes`, `--near`. Run with `-h`.
