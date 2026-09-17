# pl94

Read the Census PL 94-171 bulk file for one county, and refuse a block extract whose totals do
not reconcile to the county record in the same file.

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

pl94 filters on the geoheader's own `COUNTY` field, which every record either fills in or leaves
empty, and it will not hand you an extract until the extract equals the SUMLEV 050 county record
carried in the same download.

```
$ python pl94.py --self-test
pl94 self-test: no network, no census download, no arcpy
--------------------------------------------------------------------
PASS  a geoheader record's SUMLEV is read at 2
PASS  a fixed-width record with no pipes raises rather than reading as 97 empty fields  <-- pinned defect
PASS  STATE 11 and COUNTY 001, with the eight digit STATENS 01702382 sitting between them  <-- pinned defect
PASS  a real SUMLEV 610 state legislative district in the same file carries the SAME GEOCODE 11001 as the county  <-- pinned defect
PASS  and the seven P1 race columns of a REAL record add to it exactly, which no fixture this file wrote could have arranged
PASS  adding P0010002 to them gives 1323013 for a population of 689545  <-- pinned defect
PASS  and P1 total plus P2 Hispanic gives 767197 for the same 689545  <-- pinned defect
PASS  the seven P1 race columns add exactly to P1_001
PASS  adding P0010002 to those seven counts the one-race population twice  <-- pinned defect
PASS  a SUMLEV 620 legislative district numbered 083 has GEOCODE 12083, passes the prefix test and is NOT in county 083  <-- pinned defect
PASS  because its COUNTY field is empty, which is how the file says so
PASS  a SUMLEV 160 place with GEOCODE 1208300 passes the prefix test and is NOT in county 083  <-- pinned defect
PASS  a SUMLEV 155 record IS in county 083 even though its GEOCODE is state + place + county and fails the prefix test  <-- pinned defect
PASS  STATE is read at 12, two fields before COUNTY, not one  <-- pinned defect
PASS  three blocks of 10, 12 and 8 reconcile to a county record of 30
PASS  the same records filtered on the prefix alone sum to 127 for a county of 30  <-- pinned defect
PASS  P1 total plus P2 Hispanic is 37 for a population of 30  <-- pinned defect
PASS  a file with no SUMLEV 050 row for the county is REFUSED, not reported against nothing  <-- pinned defect
PASS  an extract of 22 against a control of 30 is REFUSED
PASS  and the difference is signed, so an over-count reads differently from an under-count of the same size  <-- pinned defect
PASS  two SUMLEV 050 rows for one county are REFUSED as ambiguous, rather than one of them being picked
PASS  a block with no segment 1 row is REFUSED, not dropped from a sum that then reconciles to nothing
PASS  a block whose P1_001 still says 10 while its race columns add to 9 leaves the county total looking right
PASS  and it is REFUSED anyway, because the row disagrees with itself  <-- pinned defect
PASS  extracting the control record and checking it against itself is REFUSED  <-- pinned defect
PASS  a LOGRECNO repeated in the geoheader raises, rather than the second record replacing the first and a block leaving the extract without changing the row count  <-- pinned defect
PASS  a LOGRECNO repeated in segment 1 raises, rather than the second population replacing the first  <-- pinned defect
PASS  while 148 raises, so the minimum is 149 and not one less  <-- pinned defect
...
PASS  the real zip reconciles through the command line, so segments 2 and 3 are passed over rather than read as more segment 1  <-- pinned defect
PASS  --out without --apply writes nothing  <-- pinned defect
PASS  and every row carries exactly twelve values, so a thirteenth column cannot ride in under a twelve column header  <-- pinned defect
PASS  no blank line between rows, which is what newline='' prevents on windows  <-- pinned defect
PASS  --apply does NOT write an extract that failed to reconcile  <-- pinned defect
PASS  and still exits 1, because the extract is still wrong  <-- pinned defect
PASS  two files that both look like the geoheader are REFUSED, not resolved by taking the first  <-- pinned defect
PASS  a truncated segment 1 record exits 2 rather than reading a short record as zeros
PASS  an extract that cannot be written exits 2, not 1, because 1 means the numbers disagreed
PASS  main() called with no argv reads sys.argv and still refuses without --pl
PASS  --allow-unreconciled is off by default  <-- pinned defect
PASS  there is no default county, because a county baked into a tool is how it confidently reports the wrong place  <-- pinned defect
PASS  there is no --year, because the 2010 fixed-width layout was never round-tripped through a real 2010 file  <-- pinned defect
PASS  the harness records a false check, a missing exception and a wrong exception as three failures, so a broken tool turns this self-test red  <-- pinned defect
--------------------------------------------------------------------
170 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `argparse`, `collections`, `csv`, `os`, `sys` and
`zipfile`, plus `contextlib`, `io`, `shutil` and `tempfile` inside the self-test. It runs on
ArcGIS Pro's Python and on a plain `python3`. `arcpy` is not used and the `arcgis` package is not
needed.

```
git clone https://github.com/uhsear/pl94.git
python pl94.py --self-test
```

`--self-test` needs no network and no Census download, so you can check the tool before you have a
file to point it at. It builds a PL zip in a temporary directory out of the same field layout the
parser reads, then runs the command line against it end to end: the extract, the refusals, the
CSV, and every exit code.

The input is a state's 2020 PL 94-171 **legacy format** zip, as published at
`www2.census.gov/programs-surveys/decennial/2020/data/01-Redistricting_File--PL_94-171/`.

## Quick start

```
python pl94.py --pl wy2020.pl.zip --county 021
```

That reads the county, prints the level table and the reconciliation, and writes nothing.

```
python pl94.py --pl wy2020.pl.zip --county 021 --out blocks.csv --apply
```

## Usage

| Flag | Default | What it does |
|---|---|---|
| `--pl` | none | The state's 2020 PL 94-171 legacy-format zip. Required. |
| `--county` | none | Three digit county FIPS, e.g. `021`. Required. There is no default. |
| `--sumlev` | `750` | Three digit summary level to extract. `750` is blocks. |
| `--out` | none | Path for the CSV extract. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--allow-unreconciled` | off | Write `--out` even when the extract does not reconcile. |
| `--self-test` | off | Run the offline assertions and exit. |

There is no `--year`. See **Limits**.

## What it checks

1. **Which records are in the county.** The geoheader's own `COUNTY` field, at position 14. A
   record that belongs to a county carries its FIPS there; one that does not leaves it empty. No
   summary level list to keep up to date, and no guessing at where a county sits inside a
   `GEOCODE` whose layout changes per level. `STATENS`, an eight digit permanent identifier, sits
   at 13, so "STATE plus one" reads the wrong field.
2. **The control total.** The single SUMLEV 050 record for that county, read out of the same zip.
   No control record, two of them, or one with no population row: refused. A published figure is
   never compiled into this tool.
3. **The extract against the control.** The requested level must sum to exactly the county
   record. Off by anything, and the CSV is not written.
4. **Every extracted record against itself.** The seven P1 race columns must add to `P0010001` on
   every row. One row that disagrees with itself refuses the whole extract, even when the county
   total still looks right.
5. **The file's shape.** The geoheader must have exactly 97 fields and segment 1 at least 149. A
   short record is a truncated download, not a row of zeros.
6. **The zip's members.** Exactly one geoheader and exactly one segment 1 file. Two of either is
   refused rather than resolved by taking the first, because a zip re-made on a Mac carries a
   `__MACOSX` copy of every file.
7. **That it is one file.** `LOGRECNO` is the key the geoheader and segment 1 are joined on, and
   the Census Bureau numbers it once per file. A repeat means two files were concatenated. Left
   alone, the second record replaces the first: a block leaves the extract while the row count
   stays plausible. pl94 refuses instead.

What the prefix filter would have done is printed beside all of this, as a number, so the
diagnostic is a measurement rather than a claim:

```
$ python pl94.py --pl dc2020.pl.zip --county 001
pl94: dc2020.pl.zip
  county 001, summary level 750

  SUMLEV     records    sum P1_001   in county 001?
  050              1        689545   yes, the county control record
  060              1        689545   yes, another cut of the same people
  140            206        689545   yes, another cut of the same people
  150            571        689545   yes, another cut of the same people
  155              1        689545   yes, and the GEOCODE prefix misses 1 of them
  510              1        689545   yes, and the GEOCODE prefix misses 1 of them
  511            206        689545   yes, and the GEOCODE prefix misses 206 of them
  610              1         85285   NO, the GEOCODE prefix let it in
  612              8        689545   yes, and the GEOCODE prefix misses 7 of them
  614              1         85285   NO, the GEOCODE prefix let it in
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

RECONCILED: the extract equals the county record in the same file.
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The extract reconciles to the county record in the same file. |
| 1 | It does not. The reasons are printed and the CSV was not written. |
| 2 | The file could not be read, or the CSV could not be written. |
| 64 | Usage error. |

Exit 1 and exit 2 are kept apart on purpose. A scheduled job reads 1 as "the numbers disagreed",
which is a fact about the data. A write that failed is a fact about the disk and needs a different
page.

## Output

`--out` with `--apply` writes twelve columns, sorted by GEOID so two runs over an unchanged file
produce the same bytes and `diff` is useful:

```
$ python pl94.py --pl wy2020.pl.zip --county 021 --out blocks.csv --apply
wrote blocks.csv (3976 row(s))

$ head -1 blocks.csv; sed -n '1590,1592p' blocks.csv
geoid,arealand,areawatr,total,white,black,aian,asian,nhpi,other,two_or_more,hispanic_any_race
560210002004009,20764,0,81,48,7,3,0,0,9,14,18
560210002004010,81854,0,169,141,2,0,0,0,13,13,27
560210002004011,110550,0,282,218,16,8,0,0,13,27,52
```

Eight of those columns add up. `total` is `P0010001`; `white` through `two_or_more` are the seven
columns that sum to it. `hispanic_any_race` is named that way because it overlaps every one of
them, and `P0010002` is not written at all. `arealand` and `areawatr` are copied through as text
and never summed, so an offset that is off by one shows up as an obviously wrong column rather
than as a wrong population.

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

- **2020 only.** There is no `--year`. The 2010 PL geoheader is fixed width rather than pipe
  delimited, and the offsets for it in the source this tool came from were never round-tripped
  through a real 2010 state file. A self-test that generated its own fixed-width fixture from
  those offsets would only be testing the fixture against itself, so 2010 is absent rather than
  untested.
- Segment 1 only, which is tables P1 and P2. Housing units are H1 in segment 2, and voting-age
  population is P3 and P4. Same file, same join on `LOGRECNO`, not read here.
- One county per run. Reconciling a whole state means one run per county, which is what the
  county control record makes checkable.
- It reconciles population, not geography. A block present in the PL file and absent from the
  matching TIGER/Line shapefile is a real and common problem, and this tool cannot see it.
- Reconciling against the county record proves the extract is complete and not double counted. It
  does not prove the Census Bureau's own numbers, and it cannot see differential privacy noise,
  which the 2020 file carries by design at block level.
- `AREALAND` and `AREAWATR` are copied, not validated. They are the only two fields taken from the
  far end of the geoheader.
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
