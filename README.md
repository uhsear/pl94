# pl94

Read the Census PL 94-171 bulk file for one county, and refuse a block extract whose population,
housing and area do not reconcile to the county record in the same file. It reads the 2020
pipe-delimited files and the 2010 fixed-width ones.

You need block-level population for a county. You download the state's PL 94-171 zip, because it
is one file and it holds everything, and you open the geoheader. There is a `GEOCODE` column, and
for a block it reads `560210002004011` and so on: state, county, tract, block. So you filter
on characters three to five and sum the population column.

That filter is wrong in both directions, and the file will not tell you.

It lets in what is not there. `GEOCODE` is laid out differently for each of the forty-odd summary
levels in the file, and several of them put three digits exactly where a county FIPS would sit. In
the real 2020 file for the District of Columbia, county 001 has `GEOCODE` `11001`, and so does
state legislative district 001. Both pass. It also leaves out what is there: summary level 155 is
state, then place, then county, so a record that really is in the county fails the same test.

Then the counting problem rides along behind it. Every level in the file covers the same people:
the county, its county subdivisions, its tracts, its block groups, its voting districts, its
blocks. Nothing in a record says "do not add this row to that one". Run that filter over the
District of Columbia file and the rows it admits sum to **12,489,830** for a county whose
population is **689,545**. Nothing errors. Nothing warns. The number is off by eighteen times and
still looks like a population.

A third trap is inside the race table. `P0010002` is not a race. It is "population of one race", a
subtotal of the six single-race columns above it, and adding it to them counts most of the county
twice. Beside it, table P2 is Hispanic or Latino **origin**, a separate question asked of everyone
P1 already counted. For the District of Columbia the seven real P1 columns add to 689,545 exactly,
while P1 total plus P2 Hispanic comes to 767,197 for a population of 689,545.

The 2010 file has the same traps in a different shape. Its geoheader is fixed width and has no
`GEOCODE` at all, so you build the key yourself. In the real 2010 Wyoming file, State Senate
District 21 builds to `56021`, which is also Laramie County.

pl94 filters on the geoheader's own `COUNTY` field, which every record either fills in or leaves
empty. It will not hand you an extract until the extract's population, H1 housing units, land
area and water area all equal the SUMLEV 050 county record carried in the same download.

```
$ python pl94.py --self-test
pl94 self-test: no network, no census download, no arcpy
--------------------------------------------------------------------
PASS  a geoheader record's SUMLEV is read at 2
PASS  a fixed-width record with no pipes raises rather than reading as 97 empty fields  <-- pinned defect
PASS  a segment 2 record of 151 fields raises, so H1 is never read out of a truncated row  <-- pinned defect
PASS  STATE 11 and COUNTY 001, with the eight digit STATENS 01702382 sitting between them  <-- pinned defect
PASS  a real SUMLEV 610 state legislative district in the same file carries the SAME GEOCODE 11001 as the county  <-- pinned defect
PASS  and the seven P1 race columns of a REAL record add to it exactly, which no fixture this file wrote could have arranged
PASS  adding P0010002 to them gives 1323013 for a population of 689545  <-- pinned defect
PASS  and P1 total plus P2 Hispanic gives 767197 for the same 689545  <-- pinned defect
PASS  the two 2010 geoheader records reassemble to the exact bytes of the published lines
PASS  a real 2010 county record reads SUMLEV 050, LOGRECNO 0046955, STATE 56 and COUNTY 021 at Figure 2-4's positions
PASS  AREALAND and AREAWATR come out of their space-padded 14 character fields as the published square metres  <-- pinned defect
PASS  a real 2010 SUMLEV 610 senate district 21 composes the SAME GEOID 56021 as the county, the 2010 twin of the DC case  <-- pinned defect
PASS  the real 2010 segment 2 record carries H1 40462 = 37576 occupied + 2886 vacant at the same three positions
PASS  a SUMLEV 620 legislative district numbered 083 has GEOCODE 12083, passes the prefix test and is NOT in county 083  <-- pinned defect
PASS  a SUMLEV 155 record IS in county 083 even though its GEOCODE is state + place + county and fails the prefix test  <-- pinned defect
PASS  three blocks of 10, 12 and 8 reconcile to a county record of 30
PASS  the same records filtered on the prefix alone sum to 127 for a county of 30  <-- pinned defect
PASS  a file with no SUMLEV 050 row for the county is REFUSED, not reported against nothing  <-- pinned defect
PASS  and the difference is signed, so an over-count reads differently from an under-count of the same size  <-- pinned defect
PASS  and it is REFUSED anyway, because the row disagrees with itself  <-- pinned defect
PASS  a LOGRECNO repeated in segment 2 raises too, rather than the second housing count replacing the first  <-- pinned defect
PASS  a block whose H1 says 6 where the county allows 5 leaves the population reconciled at 30
PASS  and is REFUSED on housing alone, 13 against 12  <-- pinned defect
PASS  an extra block of 0 people and 0 housing units leaves P1 and H1 both reconciled
PASS  and is REFUSED on AREALAND, 3500 square metres against 3000  <-- pinned defect
PASS  a 2010 SUMLEV 155 extract is REFUSED, because no GEOID rule exists for it, rather than written under an empty key  <-- pinned defect
...
PASS  --apply does NOT write an extract that failed to reconcile  <-- pinned defect
PASS  a zip with no segment 2 file exits 2, rather than reconciling population and skipping housing  <-- pinned defect
PASS  a 2010 zip reconciles through the command line with --year 2010
PASS  the same zip without --year exits 2 and names the 2010 members it does hold, so the fix is obvious  <-- pinned defect
PASS  --ap is refused by argparse with exit 2 and writes nothing, rather than being read as --apply  <-- pinned defect
PASS  the harness records a false check, a missing exception and a wrong exception as three failures, so a broken tool turns this self-test red  <-- pinned defect
--------------------------------------------------------------------
241 assertions, 0 failed
```

The same 241 assertions pass on Windows with Python 3.13, and on Ubuntu with Python 3.12 and
Python 3.9. Branch coverage of `pl94.py` under `--self-test` is 100 percent.

## Requirements

Python 3.9 or newer. Standard library only: `argparse`, `collections`, `csv`, `os`, `sys` and
`zipfile`, plus `contextlib`, `hashlib`, `io`, `shutil` and `tempfile` inside the self-test. It runs
on ArcGIS Pro's Python and on a plain `python3`. `arcpy` is not used and the `arcgis` package is
not needed.

```
git clone https://github.com/uhsear/pl94.git
python pl94.py --self-test
```

`--self-test` needs no network and no Census download, so you can check the tool before you have a
file to point it at. It builds a 2020 PL zip and a 2010 PL zip in a temporary directory, out of
the field layouts the parsers read. Then it runs the command line against them end to end: the
extract, the refusals, the CSV, and every exit code.

The input is a state's PL 94-171 **legacy format** zip, as published by the Census Bureau:

- 2020: `www2.census.gov/programs-surveys/decennial/2020/data/01-Redistricting_File--PL_94-171/`
- 2010: `www2.census.gov/census_2010/01-Redistricting_File--PL_94-171/`

## Quick start

```
python pl94.py --pl wy2020.pl.zip --county 021
python pl94.py --pl wy2010.pl.zip --county 021 --year 2010
```

Each reads the county, prints the level table and the reconciliation, and writes nothing.

```
python pl94.py --pl wy2020.pl.zip --county 021 --out blocks.csv --apply
```

## Usage

| Flag | Default | What it does |
|---|---|---|
| `--pl` | none | The state's PL 94-171 legacy-format zip. Required. |
| `--county` | none | Three digit county FIPS, e.g. `021`. Required. There is no default. |
| `--year` | `2020` | The census year of `--pl`: `2020` (pipe delimited) or `2010` (fixed width). |
| `--sumlev` | `750` | Three digit summary level to extract. `750` is blocks. |
| `--out` | none | Path for the CSV extract. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--allow-unreconciled` | off | Write `--out` even when the extract does not reconcile. |
| `--self-test` | off | Run the offline assertions and exit. |

Flags must be spelled out. A prefix such as `--ap` is refused, not read as `--apply`.

`--year` defaults to `2020` because that is the only layout earlier releases read. A 2010 zip read
without `--year 2010` exits 2, and the error lists the members the zip holds, so `wygeo2010.pl` is
in plain sight.

## What it checks

1. **Which records are in the county.** The geoheader's own `COUNTY` field: position 14 of the
   2020 record, characters 30 to 32 of the 2010 one. A record that belongs to a county carries
   its FIPS there; one that does not leaves it empty. No summary level list to keep up to date,
   and no guessing at where a county sits inside a key whose layout changes per level. In 2020,
   `STATENS`, an eight digit permanent identifier, sits at 13, so "STATE plus one" reads the wrong
   field.
2. **The control record.** The single SUMLEV 050 record for that county, read out of the same zip.
   No control record, two of them, or one with no segment 1 or segment 2 row: refused. A published
   figure is never compiled into this tool.
3. **The extract against the control, four ways.** The requested level must sum to exactly the
   county record's `P0010001` population, its `H0010001` housing units, its `AREALAND` and its
   `AREAWATR`. Off by anything on any of the four, and the CSV is not written. The areas catch
   what the counts cannot: a block with no people and no homes moves neither total, so a filter
   that drops empty blocks reconciles on both. Only the land area sees those blocks go.
4. **Every extracted record against itself.** The seven P1 race columns must add to `P0010001`,
   and H1 occupied plus vacant must add to the housing total, on every row. One row that disagrees
   with itself refuses the whole extract, even when the county totals still look right.
5. **The file's shape.** The 2020 geoheader must have exactly 97 fields. The 2010 geoheader must
   be exactly 500 characters with no pipe in it. Segment 1 needs at least 149 fields and segment 2
   at least 152. A short record is a truncated download, not a row of zeros, and an area field
   that is not a whole number raises rather than adding as zero.
6. **The zip's members.** Exactly one geoheader, one segment 1 and one segment 2 file for the year
   asked. Two of any of them is refused rather than resolved by taking the first, because a zip
   re-made on a Mac carries a `__MACOSX` copy of every file. The 2020 segment 3, group quarters,
   is not read.
7. **That it is one file.** `LOGRECNO` is the key the geoheader and the segments are joined on,
   and the Census Bureau numbers it once per file. A repeat means two files were concatenated.
   Left alone, the second record replaces the first: a block leaves the extract while the row
   count stays plausible. pl94 refuses instead.

What the prefix filter would have done is printed beside all of this, as a number, so the
diagnostic is a measurement rather than a claim:

```
$ python pl94.py --pl dc2020.pl.zip --county 001
pl94: dc2020.pl.zip
  county 001, summary level 750, 2020 layout

  SUMLEV     records    sum P1_001   in county 001?
  050              1        689545   yes, the county control record
  060              1        689545   yes, another cut of the same people
  140            206        689545   yes, another cut of the same people
  150            571        689545   yes, another cut of the same people
  155              1        689545   yes, and the GEOCODE prefix misses 1 of them
  510              1        689545   yes, and the GEOCODE prefix misses 1 of them
  511            206        689545   yes, and the GEOCODE prefix misses 206 of them
  512              1        689545   yes, another cut of the same people
  521              1        689545   yes, and the GEOCODE prefix misses 1 of them
  610              1         85285   NO, the GEOCODE prefix let it in
  612              8        689545   yes, and the GEOCODE prefix misses 7 of them
  ...
  740            789        689545   yes, another cut of the same people
  750           6012        689545   yes, EXTRACTED

  sum over every level the GEOCODE prefix admits: 12489830
    Not a population, and pl94 never reports it as one. The
    levels marked yes nest, so they count the same people
    several times over, and the levels marked NO are not in
    this county at all.

  race columns over the extract
    P1_001 total                      : 689545
    the seven P1 race columns add to  : 689545
    P0010002 population of one race   : 633468  (a SUBTOTAL)
    P2 Hispanic origin, any race      : 77652
    P1_001 + P2 Hispanic              : 767197  for a population of 689545
    P2 asks the origin of everyone P1 already counted, so the
    two do not add. The extract calls that column
    hispanic_any_race and pl94 never totals it with the rest.

  county control record 11001       : 689545
  summary level 750 over 6012 record(s) : 689545
  H1 housing units  extract / county : 350364 / 350364
  AREALAND sq m     extract / county : 158316124 / 158316124
  AREAWATR sq m     extract / county : 18709762 / 18709762

RECONCILED: the extract equals the county record in the same file.
```

## The 2010 layout

The 2010 geoheader is fixed width: 500 characters per record and no delimiter. Numbers are
right-aligned and padded with spaces, so pl94 strips every field as it cuts it. Segments 1 and 2
are comma delimited and otherwise have the same shape as in 2020: P1 and P2 in segment 1, then
P3, P4 and H1 in segment 2.

There is no `GEOCODE` field in 2010. pl94 composes the key per summary level, using the
concatenations in the Census Bureau's GEOID construction note: blocks are `STATE + COUNTY +
TRACT + BLOCK`, tracts are `STATE + COUNTY + TRACT`, and so on. The note does not list 040 and
050; pl94 uses `STATE` and `STATE + COUNTY` for them. A level that has no rule composes no key,
and pl94 refuses to extract it. The level table names the composed GEOID instead of `GEOCODE`:

```
$ python pl94.py --pl wy2010.pl.zip --county 021 --year 2010
pl94: wy2010.pl.zip
  county 021, summary level 750, 2010 layout

  SUMLEV     records    sum P1_001   in county 021?
  050              1         91738   yes, the county control record
  ...
  610              1         18170   NO, the composed GEOID prefix let it in
  ...
  750           5168         91738   yes, EXTRACTED
  970              2         13833   NO, the composed GEOID prefix let it in

  sum over every level the composed GEOID prefix admits: 593397
  ...
  county control record 56021       : 91738
  summary level 750 over 5168 record(s) : 91738
  H1 housing units  extract / county : 40462 / 40462
  AREALAND sq m     extract / county : 6956479515 / 6956479515
  AREAWATR sq m     extract / county : 4269807 / 4269807

RECONCILED: the extract equals the county record in the same file.
```

The layout was checked against two sources and one real file:

- The field positions are those in Figure 2-4, "Geographic Header Record", of the *2010 Census
  Redistricting Data (Public Law 94-171) Summary File Technical Documentation*, PL/10-2 (RV),
  `www2.census.gov/programs-surveys/decennial/rdo/about/2010-census-programs/2010Census_pl94-171_techdoc.pdf`.
  The Bureau's SAS import program, `pl_all_3_2010.sas` in the 2010 directory above, gives the
  same positions and the comma delimiter.
- The key rules are from `0GEOID_Construction_for_Matching.pdf` in the same 2010 directory.
- Against the real `wy2010.pl.zip`, every one of Wyoming's 23 counties reconciles on all four
  totals at summary level 750. So does every county in `wy2020.pl.zip`, and the District of
  Columbia in `dc2020.pl.zip`. These runs gave the same result on Windows and on Linux.
- For Laramie County, 021, the 5,168 composed 2010 block GEOIDs match `GEOID10` in the 2010
  TIGER/Line file `tl_2010_56021_tabblock10` one for one. `AREALAND` and `AREAWATR` equal
  `ALAND10` and `AWATER10` on every block. In the same county, levels 060, 140, 150 and 700
  reconcile in both years.

The self-test pins the layout with four real 2010 Wyoming records: the county's geoheader record,
a senate district's geoheader record, and the county's segment 1 and segment 2 rows. Two of them are 500
characters, mostly spaces. The self-test writes them as runs of text and runs of spaces, and it
checks each one against the SHA-1 of the published line.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The extract reconciles to the county record in the same file. |
| 1 | It does not. The reasons are printed and the CSV was not written. |
| 2 | The file could not be read, or the CSV could not be written. argparse also exits 2 for a flag it does not know, such as `--ap`. |
| 64 | Usage error found by pl94: a missing `--pl` or `--county`, a malformed `--county`, `--sumlev` or `--year`, or `--apply` without `--out`. |

Exit 1 and exit 2 are kept apart on purpose. A scheduled job reads 1 as "the numbers disagreed",
which is a fact about the data. A write that failed is a fact about the disk and needs a different
page.

## Output

`--out` with `--apply` writes fifteen columns, sorted by GEOID so two runs over an unchanged file
produce the same bytes and `diff` is useful:

```
$ python pl94.py --pl wy2020.pl.zip --county 021 --out blocks.csv --apply
wrote blocks.csv (3976 row(s))

$ head -1 blocks.csv; sed -n '179,181p' blocks.csv
geoid,arealand,areawatr,total,white,black,aian,asian,nhpi,other,two_or_more,hispanic_any_race,housing_units,occupied,vacant
560210002004009,20764,0,81,48,7,3,0,0,9,14,18,31,31,0
560210002004010,81854,0,169,141,2,0,0,0,13,13,27,106,105,1
560210002004011,110550,0,282,218,16,8,0,0,13,27,52,137,125,12
```

Eight of those columns add up as population. `total` is `P0010001`; `white` through
`two_or_more` are the seven columns that sum to it. `hispanic_any_race` is named that way because
it overlaps every one of them, and `P0010002` is not written at all. `housing_units` is
`H0010001`, and `occupied` plus `vacant` equals it on every row. `arealand` and `areawatr` are in
square metres, copied as text. A 2010 extract has the same columns, with the spaces stripped from
its areas.

## Why the obvious version is wrong

The Census Bureau's own API is the obvious tool and it is the right one for most work.
`api.census.gov/data/2020/dec/pl` takes a geography clause, so SUMLEV never comes up, and `cenpy`
and `censusdata` wrap it in a dataframe. The gap is bulk and offline. The API caps variables per
call and paginates geography, so every block in a state is hundreds of requests against a live
service, and a key that expires mid-run leaves a partial answer that looks whole. The bulk zip is
one download and is complete. It just will not tell you which of its rows may be added together.

The handwritten version has two bugs and the self-test pins both:

```python
if len(geocode) < 5 or geocode[2:5] != county_fips:   # wrong in both directions
    continue
total += int(fields[P1_TOTAL])                        # over every level at once
```

The subtle one is the refusal. When the block sum does not match the county record, the tempting
move is to report the blocks and mention it:

```python
if block_sum != county_total:
    log.warning("block sum %d != county %d", block_sum, county_total)  # wrong
write_csv(dest, blocks)
```

The CSV still gets written, still gets loaded into a map, and the warning scrolls off. pl94
refuses to write it, and `--allow-unreconciled` still exits 1, so a job that overrides the refusal
cannot also report success.

## Limits

- **2010 and 2020 only.** The 2000 PL file has another layout, and pl94 refuses any other
  `--year`.
- **2010 keys exist only for the levels in the GEOID construction note**, plus 040 and 050.
  Summary level 155 (place within county) and the American Indian area levels have no rule, so a
  2010 extract of them is refused. They still appear in the level table, where the composed GEOID
  prefix "misses" them because they have no key at all.
- Segments 1 and 2 only. pl94 reads P1, P2 and H1. Voting-age population is P3 and P4 in segment
  2, and pl94 does not read it. The 2020 segment 3, group quarters, is not read either.
- One county per run. Reconciling a whole state means one run per county, which is what the
  county control record makes checkable.
- It reconciles counts and areas, not shapes. The area check proves that the extract's blocks
  account for the county's square metres. It does not prove that each block in the PL file has a
  polygon in the matching TIGER/Line shapefile. pl94 cannot see geometry.
- Reconciling against the county record proves the extract is complete and not double counted. It
  does not prove the Census Bureau's own numbers, and it cannot see differential privacy noise,
  which the 2020 file carries by design at block level.
- Exit 2 has two causes: an unreadable file, and a flag argparse does not know, such as `--ap`.
  argparse exits before pl94 sees the arguments. Read stderr to tell them apart.
- The tool makes no network calls, so there is no network path to test against a stub server.
- The whole county's admitted records are held in memory. A large county is a few hundred thousand
  small tuples, which is fine; the whole state at once would not be.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [geocodesift](https://github.com/uhsear/geocodesift) - the same refusal to trust a number nobody checked
- [nalmatch](https://github.com/uhsear/nalmatch) - joining public records to local parcels, where the key is the hard part
- [roadmiles](https://github.com/uhsear/roadmiles) - a total that has to be defensible to somebody who did not run it
