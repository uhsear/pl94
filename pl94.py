#!/usr/bin/env python
"""Read the Census PL 94-171 bulk file for one county and refuse a block extract that does not reconcile to its county record.

The PL 94-171 redistricting file is one flat file holding every summary level
the Census Bureau published for a state: the state itself, its counties, its
tracts, its block groups, its blocks, its places, its legislative districts.
Nothing in a record says "do not add this row to that one". Sum the file
without filtering on SUMLEV and a county population inflates by a multiple
that depends on which levels the file carries. Nothing errors, and the answer
is still the right order of magnitude, which is what makes it survive review.

The Census Bureau's own API is the obvious tool and it is the right one for
most work. api.census.gov/data/2020/dec/pl takes a geography clause, so
SUMLEV never comes up, and cenpy and censusdata wrap it in a dataframe. The
gap is bulk and offline: the API caps variables per call and paginates
geography, so every block in a state is hundreds of requests against a live
service, and a key that expires mid-run leaves a partial answer that looks
whole. The bulk zip is one download and is complete. This tool reads that zip
and does the one thing the file format will not do for you: it proves the
extract against the county control record carried in the same file, and
refuses to write an extract that does not match.

It is read-only against the PL file. The only thing it writes is a CSV
extract, and that needs --apply.

    python pl94.py --self-test
    python pl94.py --pl wy2020.pl.zip --county 021
    python pl94.py --pl wy2020.pl.zip --county 021 --out blocks.csv --apply

Exit codes: 0 the extract reconciles, 1 it does not and pl94 refused it,
2 the file could not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import csv
import os
import sys
import zipfile

# =============================================================================
# CONFIGURATION. Deliberately not constants at the call site.
# =============================================================================

# The 2020 legacy-format geoheader is pipe delimited with 97 fields. These are
# 0-based positions in that record.
GEO_SEP = "|"
GEO_FIELDS = 97
GEO_SUMLEV = 2
GEO_LOGRECNO = 7
GEO_GEOCODE = 9

# STATE and COUNTY, each blank on a record that has no such parent. COUNTY is
# the field this whole tool turns on: a record that belongs to a county says
# so here, and a record that does not leaves it empty. Note the gap. STATENS,
# an eight digit GNIS code, sits between them at 13, so the obvious guess of
# "STATE plus one" reads a permanent identifier as a county FIPS.
GEO_STATE = 12
GEO_COUNTY = 14

# AREALAND and AREAWATR sit near the end of the 97 fields. They are carried
# through the extract as TEXT and never summed or compared, so an offset that
# is off by one shows up as an obviously wrong column rather than as a wrong
# population. Every number pl94 reasons about comes from segment 1.
GEO_AREALAND = 84
GEO_AREAWATR = 85

# Segment 1 is pipe delimited with 149 fields: 5 header fields, then table P1
# (71 fields) and table P2 (73 fields). A shorter record is a truncated file.
SEG_SEP = "|"
SEG1_FIELDS = 5 + 71 + 73
SEG1_LOGRECNO = 4

# P1 is the race table. P0010001 is the total, and the SEVEN single-race and
# two-or-more columns below add up to it exactly.
P1_TOTAL = 5                                  # P0010001
P1_WHITE, P1_BLACK, P1_AIAN = 7, 8, 9         # P0010003 to P0010005
P1_ASIAN, P1_NHPI, P1_OTHER = 10, 11, 12      # P0010006 to P0010008
P1_TWO = 13                                   # P0010009

# P0010002 is "population of one race". It is a SUBTOTAL of the six
# single-race columns, not a category beside them. Add it to the parts and the
# one-race population is counted twice, which is a clean doubling that no
# reconciliation against a state total would catch.
P1_ONE_RACE = 6

# P2 is Hispanic or Latino ORIGIN. It is a separate question asked of every
# person P1 already counted, so P1 and P2 overlap completely and do not add.
P2_HISPANIC = 5 + 71 + 1                      # P0020002

# The county record pl94 reconciles against. It is in the same file as the
# blocks, published by the same agency in the same run, which is the whole
# reason it is a usable control: no second download, no second vintage, and
# no published figure copied into the source of a tool.
CONTROL_SUMLEV = "050"

# Blocks. The level almost everybody wants out of a PL file.
DEFAULT_SUMLEV = "750"

# Columns of the extract, in order. hispanic_any_race is named that way on
# purpose: it overlaps every race column beside it, and a spreadsheet that
# sums a row of this CSV must not be invited to include it.
CSV_FIELDS = ("geoid", "arealand", "areawatr", "total", "white", "black",
              "aian", "asian", "nhpi", "other", "two_or_more",
              "hispanic_any_race")

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

GeoRow = collections.namedtuple("GeoRow",
                                "sumlev logrecno geocode state county "
                                "arealand areawatr")

Counts = collections.namedtuple("Counts",
                                "total one_race white black aian asian nhpi "
                                "other two hispanic")

LevelRow = collections.namedtuple("LevelRow",
                                  "sumlev records p1_total in_county "
                                  "prefix_records")

Report = collections.namedtuple("Report",
                                "county sumlev control_geoid control_total "
                                "extract_total extract_rows by_level "
                                "prefix_only_total one_race_total "
                                "race_parts_total hispanic_total bad_race "
                                "reasons reconciled")

# The seven P1 columns that add up to P1_001. one_race is not among them.
RACE_PARTS = ("white", "black", "aian", "asian", "nhpi", "other", "two")


class MalformedRecord(ValueError):
    """A record in the PL file is not the shape the layout says it is.

    A ValueError rather than a warning, because the alternative is to skip the
    record and carry on, and a skipped block is a population that quietly
    vanishes from an extract that then reconciles against nothing.
    """


class MissingMember(ValueError):
    """The zip does not hold exactly one file of the kind wanted."""


# ------------------------------------------------------------------- parsing

def _line(raw):
    """One record as text. PL files are latin-1 and CRLF terminated."""
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    return raw.rstrip("\r\n")


def parse_geo_line(line):
    """Return a GeoRow from one 2020 legacy-format geoheader record."""
    f = line.split(GEO_SEP)
    if len(f) != GEO_FIELDS:
        # An exact count, not a minimum. A 2010 file fed to this parser is one
        # fixed-width field with no pipes in it at all, and would otherwise
        # read as a geoheader whose every value is empty.
        raise MalformedRecord(
            "geoheader record has %d fields, the 2020 layout has %d"
            % (len(f), GEO_FIELDS))
    return GeoRow(f[GEO_SUMLEV].strip(), f[GEO_LOGRECNO].strip(),
                  f[GEO_GEOCODE].strip(), f[GEO_STATE].strip(),
                  f[GEO_COUNTY].strip(), f[GEO_AREALAND].strip(),
                  f[GEO_AREAWATR].strip())


def parse_seg1_line(line):
    """Return (logrecno, Counts) from one segment 1 record."""
    f = line.split(SEG_SEP)
    if len(f) < SEG1_FIELDS:
        raise MalformedRecord(
            "segment 1 record has %d fields, expected %d. The file is "
            "truncated." % (len(f), SEG1_FIELDS))
    idx = (P1_TOTAL, P1_ONE_RACE, P1_WHITE, P1_BLACK, P1_AIAN, P1_ASIAN,
           P1_NHPI, P1_OTHER, P1_TWO, P2_HISPANIC)
    try:
        values = [int(f[i]) for i in idx]
    except ValueError:
        raise MalformedRecord(
            "segment 1 record %r carries a count that is not a number"
            % (f[SEG1_LOGRECNO],))
    return f[SEG1_LOGRECNO].strip(), Counts(*values)


# ------------------------------------------------------------------ the gate

def prefix_only_match(geocode, county_fips):
    """True when characters 3 to 5 of GEOCODE are this county FIPS.

    This is the filter on its own, and on its own it is wrong. It is kept
    here, named, because the whole diagnostic rests on showing what it admits:
    a state legislative district numbered 001 has GEOCODE 11001, and so does
    the county 001 beside it. Both pass this test and only one is a county.
    It is wrong in the other direction too: SUMLEV 155 is state + place +
    county, so a record that IS in the county fails this test.
    """
    return len(geocode) >= 5 and geocode[2:5] == county_fips


def in_county(geo, county_fips):
    """The correct filter: the geoheader's own COUNTY field.

    Every record that belongs to a county carries its FIPS here, and every
    record that does not leaves the field empty. No summary level list to
    keep up to date, and no guessing at where a county sits inside a GEOCODE
    that is laid out differently for each of the forty-odd levels in the file.
    """
    return bool(county_fips) and geo.county == county_fips


def race_parts_sum(counts):
    """The seven P1 columns that add up to P1_001. Never eight."""
    return sum(getattr(counts, name) for name in RACE_PARTS)


# ---------------------------------------------------------------- the report

def build_report(loose, counts, county_fips, sumlev):
    """Reconcile an extract against the county control record.

    loose  -- {logrecno: GeoRow} for every record EITHER filter let through,
              so the level table can show what each of them admits.
    counts -- {logrecno: Counts} for those of them that have a segment 1 row.
    """
    per_level = {}
    prefix_only_total = 0
    for logrec, geo in loose.items():
        row = counts.get(logrec)
        population = row.total if row is not None else 0
        mine = in_county(geo, county_fips)
        prefixed = prefix_only_match(geo.geocode, county_fips)
        tally = per_level.setdefault((geo.sumlev, mine), [0, 0, 0])
        tally[0] += 1
        tally[1] += population
        if prefixed:
            tally[2] += 1
            prefix_only_total += population
    by_level = [LevelRow(key[0], per_level[key][0], per_level[key][1],
                         key[1], per_level[key][2])
                for key in sorted(per_level, key=lambda k: (k[0], not k[1]))]

    reasons = []

    controls = [(geo, counts.get(logrec))
                for logrec, geo in sorted(loose.items())
                if geo.sumlev == CONTROL_SUMLEV and in_county(geo,
                                                              county_fips)]
    control_geoid, control_total = None, None
    if not controls:
        reasons.append(
            "this file carries no SUMLEV %s record for county %s, so there "
            "is nothing in it to reconcile against"
            % (CONTROL_SUMLEV, county_fips))
    elif len(controls) > 1:
        reasons.append(
            "%d SUMLEV %s records match county %s (%s), so the control total "
            "is ambiguous"
            % (len(controls), CONTROL_SUMLEV, county_fips,
               ", ".join(geo.geocode for geo, _ in controls)))
    else:
        control_geoid = controls[0][0].geocode
        if controls[0][1] is None:
            reasons.append(
                "the county control record %s has no segment 1 row, so it "
                "carries no population" % control_geoid)
        else:
            control_total = controls[0][1].total

    if sumlev == CONTROL_SUMLEV:
        # Checking the control record against itself always reconciles and
        # proves nothing, which is worse than refusing.
        reasons.append(
            "summary level %s IS the control record, so checking it against "
            "itself proves nothing" % sumlev)

    wanted = [(geo.geocode, geo, logrec) for logrec, geo in loose.items()
              if geo.sumlev == sumlev and in_county(geo, county_fips)]
    if not wanted:
        # Named levels, not a whitelist. The file says which levels it carries
        # for this county, so there is nothing here to keep up to date.
        carried = sorted(set(geo.sumlev for geo in loose.values()
                             if in_county(geo, county_fips)))
        reasons.append(
            "no record in county %s carries summary level %s. This file "
            "carries %s for it." % (county_fips, sumlev,
                                    ", ".join(carried) or "no level at all"))
    orphans = sorted(geoid for geoid, _, logrec in wanted
                     if logrec not in counts)
    if orphans:
        reasons.append(
            "%d summary level %s record(s) have no segment 1 row, so their "
            "population is unknown: %s"
            % (len(orphans), sumlev, ", ".join(orphans[:3])))

    extract_rows = sorted(((geoid, geo, counts[logrec])
                           for geoid, geo, logrec in wanted
                           if logrec in counts),
                          key=lambda row: row[0])

    extract_total = sum(row[2].total for row in extract_rows)
    one_race_total = sum(row[2].one_race for row in extract_rows)
    race_parts_total = sum(race_parts_sum(row[2]) for row in extract_rows)
    hispanic_total = sum(row[2].hispanic for row in extract_rows)

    bad_race = [row[0] for row in extract_rows
                if race_parts_sum(row[2]) != row[2].total]
    if bad_race:
        reasons.append(
            "%d record(s) whose seven P1 race columns do not add to P1_001: "
            "%s" % (len(bad_race), ", ".join(bad_race[:3])))

    if control_total is not None and extract_total != control_total:
        reasons.append(
            "summary level %s sums to %d, the county control record %s says "
            "%d, a difference of %+d"
            % (sumlev, extract_total, control_geoid, control_total,
               extract_total - control_total))

    return Report(county=county_fips, sumlev=sumlev,
                  control_geoid=control_geoid, control_total=control_total,
                  extract_total=extract_total, extract_rows=extract_rows,
                  by_level=by_level, prefix_only_total=prefix_only_total,
                  one_race_total=one_race_total,
                  race_parts_total=race_parts_total,
                  hispanic_total=hispanic_total, bad_race=bad_race,
                  reasons=reasons, reconciled=not reasons)


def scan(geo_lines, seg_lines, county_fips, sumlev=DEFAULT_SUMLEV):
    """Read a geoheader and a segment 1 stream and reconcile one county."""
    if not (len(county_fips) == 3 and county_fips.isdigit()):
        raise ValueError("county FIPS must be three digits, got %r"
                         % (county_fips,))
    if not (len(sumlev) == 3 and sumlev.isdigit()):
        raise ValueError("summary level must be three digits, got %r"
                         % (sumlev,))

    loose = {}
    for raw in geo_lines:
        line = _line(raw)
        if not line.strip():
            continue
        geo = parse_geo_line(line)
        # Held when EITHER filter takes it. The correct one decides the
        # extract; the prefix-only one has to stay visible in the level
        # table, or the diagnostic is a claim instead of a measurement.
        if (in_county(geo, county_fips)
                or prefix_only_match(geo.geocode, county_fips)):
            if geo.logrecno in loose:
                # LOGRECNO is the join key and the Census Bureau numbers it
                # once per file. A repeat means two files were concatenated,
                # usually two states into one. Without this, the second
                # record replaces the first in the dictionary: a block leaves
                # the extract, the row count stays plausible, and the total
                # only moves if the two happened to differ.
                raise MalformedRecord(
                    "LOGRECNO %s appears twice in the geoheader, as GEOCODE "
                    "%s and as %s. Two files have been joined together."
                    % (geo.logrecno, loose[geo.logrecno].geocode,
                       geo.geocode))
            loose[geo.logrecno] = geo

    counts = {}
    for raw in seg_lines:
        line = _line(raw)
        if not line.strip():
            continue
        logrec, row = parse_seg1_line(line)
        if logrec in loose:
            if logrec in counts:
                # Same reason, and worse: the population itself is the thing
                # being replaced.
                raise MalformedRecord(
                    "LOGRECNO %s appears twice in segment 1, carrying %d and "
                    "then %d. Two files have been joined together."
                    % (logrec, counts[logrec].total, row.total))
            counts[logrec] = row

    return build_report(loose, counts, county_fips, sumlev)


def describe(report, source):
    """The report as printable lines."""
    out = ["pl94: %s" % source,
           "  county %s, summary level %s" % (report.county, report.sumlev),
           "",
           "  %-8s%10s%14s   %s"
           % ("SUMLEV", "records", "sum P1_001", "in county %s?"
              % report.county)]
    for level in report.by_level:
        if not level.in_county:
            note = "NO, the GEOCODE prefix let it in"
        elif level.prefix_records < level.records:
            note = ("yes, and the GEOCODE prefix misses %d of them"
                    % (level.records - level.prefix_records))
        elif level.sumlev == report.sumlev:
            note = "yes, EXTRACTED"
        elif level.sumlev == CONTROL_SUMLEV:
            note = "yes, the county control record"
        else:
            note = "yes, another cut of the same people"
        out.append("  %-8s%10d%14d   %s"
                   % (level.sumlev, level.records, level.p1_total, note))

    out.append("")
    out.append("  sum over every level the GEOCODE prefix admits: %d"
               % report.prefix_only_total)
    out.append("    Not a population, and pl94 never reports it as one. The")
    out.append("    levels marked yes nest, so they count the same people")
    out.append("    several times over, and the levels marked NO are not in")
    out.append("    this county at all.")

    out.append("")
    out.append("  race columns over the extract")
    out.append("    P1_001 total                      : %d"
               % report.extract_total)
    out.append("    the seven P1 race columns add to  : %d"
               % report.race_parts_total)
    out.append("    P0010002 population of one race   : %d  (a SUBTOTAL)"
               % report.one_race_total)
    out.append("    P2 Hispanic origin, any race      : %d"
               % report.hispanic_total)
    out.append("    P1_001 + P2 Hispanic              : %d  for a population "
               "of %d" % (report.extract_total + report.hispanic_total,
                          report.extract_total))
    out.append("    P2 asks the origin of everyone P1 already counted, so the")
    out.append("    two do not add. The extract calls that column")
    out.append("    hispanic_any_race and pl94 never totals it with the rest.")

    out.append("")
    if report.control_total is None:
        out.append("  county control record             : none")
    else:
        out.append("  county control record %-11s : %d"
                   % (report.control_geoid, report.control_total))
    out.append("  summary level %s over %d record(s) : %d"
               % (report.sumlev, len(report.extract_rows),
                  report.extract_total))
    out.append("")
    if report.reconciled:
        out.append("RECONCILED: the extract equals the county record in the "
                   "same file.")
    else:
        out.append("REFUSED:")
        for reason in report.reasons:
            out.append("  - %s" % reason)
    return out


# ----------------------------------------------------------------- self-test

def self_test():
    """Assertions over the decision core and the command line.

    No network, no database, no arcpy, no Census download. The PL zip the
    command line half runs against is built here, in a temporary directory,
    out of the same field layout the parser reads.
    """
    import contextlib
    import io
    import shutil
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("pl94 self-test: no network, no census download, no arcpy")
    print("-" * 68)

    # ---- fixture builders. Real records in the real field layout.

    # Both builders write their fields at LITERAL positions, never at the
    # constants the parser reads them back with. A fixture written through
    # GEO_COUNTY and read through GEO_COUNTY agrees with itself whatever that
    # constant happens to say, so it pins nothing: change 14 to 13 and every
    # assertion still passes while the tool reads STATENS as a county FIPS.
    # The positions below are the published 2020 legacy layout, and the real
    # Census records further down are what says so.

    def geo_line(sumlev, logrec, geocode, county="", land="1000", water="0"):
        f = [""] * 97
        f[0], f[1] = "PLST", "FL"
        f[2] = sumlev
        f[7] = logrec
        f[9] = geocode
        f[12] = "12"
        f[13] = "00294478"          # STATENS, the field that is NOT the county
        f[14] = county
        f[84] = land
        f[85] = water
        return "|".join(f)

    def seg_line(logrec, white, black, aian, asian, nhpi, other, two, hisp,
                 total=None):
        one = white + black + aian + asian + nhpi + other
        f = ["0"] * 149
        f[0], f[1] = "PLST", "FL"
        f[4] = logrec
        f[5] = str(one + two if total is None else total)
        f[6] = str(one)
        for pos, value in ((7, white), (8, black), (9, aian), (10, asian),
                           (11, nhpi), (12, other), (13, two)):
            f[pos] = str(value)
        f[77] = str(hisp)
        return "|".join(f)

    # One synthetic state file, laid out like the real ones. County 083 holds
    # three blocks of 10, 12 and 8. Every level over them repeats the same 30
    # people. Two levels that are not in the county at all carry 083 where a
    # county FIPS would sit, and one level that IS in the county does not.
    geo_rows = [
        geo_line("040", "0000001", "12"),                      # state
        geo_line("050", "0000002", "12083", "083"),            # the control
        geo_line("140", "0000003", "12083000100", "083"),      # tract
        geo_line("150", "0000004", "120830001001", "083"),     # block group
        # Listed OUT of GEOID order on purpose. Three blocks written in
        # ascending order would sort into the order they were read, and
        # an assertion about sorting would pass with no sort at all.
        geo_line("750", "0000005", "120830001001003", "083"),  # block
        geo_line("750", "0000006", "120830001001001", "083"),  # block
        geo_line("750", "0000007", "120830001001002", "083"),  # block
        geo_line("620", "0000008", "12083"),                   # SLDL 083
        geo_line("160", "0000009", "1208300"),                 # place 08300
        geo_line("155", "0000012", "1254000083", "083"),       # place-county
        geo_line("050", "0000010", "12081", "081"),            # next county
        geo_line("750", "0000011", "120810001001001", "081"),  # its block
    ]
    seg_rows = [
        seg_line("0000001", 120, 40, 4, 16, 2, 8, 10, 30),  # state, 200
        seg_line("0000002", 16, 6, 1, 2, 1, 2, 2, 7),       # control, 30
        seg_line("0000003", 16, 6, 1, 2, 1, 2, 2, 7),       # tract, 30
        seg_line("0000004", 16, 6, 1, 2, 1, 2, 2, 7),       # block group, 30
        seg_line("0000005", 5, 2, 1, 1, 0, 0, 1, 3),        # block, 10
        seg_line("0000006", 7, 3, 0, 1, 1, 0, 0, 2),        # block, 12
        seg_line("0000007", 4, 1, 0, 0, 0, 2, 1, 2),        # block, 8
        seg_line("0000008", 3, 1, 0, 0, 0, 1, 0, 1),        # SLDL 083, 5
        seg_line("0000009", 1, 1, 0, 0, 0, 0, 0, 0),        # place, 2
        seg_line("0000012", 16, 6, 1, 2, 1, 2, 2, 7),       # place-county, 30
        seg_line("0000010", 50, 20, 4, 10, 1, 8, 6, 20),    # next county, 99
        seg_line("0000011", 50, 20, 4, 10, 1, 8, 6, 20),    # its block, 99
    ]

    def without_seg(logrec):
        return [s for s in seg_rows
                if s.split("|")[4] != logrec]

    # ---- the record parsers

    row = parse_geo_line(geo_rows[4])
    check(row.sumlev == "750", "a geoheader record's SUMLEV is read at 2")
    check(row.logrecno == "0000005", "its LOGRECNO is read at 7")
    check(row.geocode == "120830001001003", "its GEOCODE is read at 9")
    check((row.arealand, row.areawatr) == ("1000", "0"),
          "AREALAND and AREAWATR are read as text, never as numbers")
    raises(lambda: parse_geo_line("12|083"),
           "a geoheader record with 2 fields instead of 97 raises")
    raises(lambda: parse_geo_line(geo_rows[4] + "|extra"),
           "a geoheader record with 98 fields raises, because the layout is "
           "exact")
    raises(lambda: parse_geo_line("PLST FL750  0000005120830001001001"),
           "a fixed-width record with no pipes raises rather than reading as "
           "97 empty fields  <-- pinned defect")

    logrec, counts = parse_seg1_line(seg_rows[4])
    check(logrec == "0000005", "a segment 1 record's LOGRECNO is read at 4")
    check(counts.total == 10, "P0010001 is read at 5")
    check(counts.one_race == 9,
          "P0010002 is read at 6 and kept apart from the parts")
    check((counts.white, counts.black, counts.aian) == (5, 2, 1),
          "P0010003 to P0010005 are read at 7, 8 and 9")
    check((counts.asian, counts.nhpi, counts.other, counts.two)
          == (1, 0, 0, 1),
          "P0010006 to P0010009 are read at 10 through 13")
    check(counts.hispanic == 3, "P0020002 is read at 5 + 71 + 1")
    raises(lambda: parse_seg1_line("|".join(["0"] * 100)),
           "a truncated segment 1 record of 100 fields raises")
    broken = seg_rows[4].split("|")
    broken[5] = ""
    raises(lambda: parse_seg1_line("|".join(broken)),
           "a segment 1 record with an empty count raises rather than "
           "reading it as zero")

    # ---- three real records, copied byte for byte out of the published 2020
    # PL 94-171 file for the District of Columbia. They are the only lines in
    # this self-test the fixture builders above did not write, and they are
    # what says the field positions are the Census Bureau's and not this
    # tool's. Public data: dc2020.pl.zip, members dcgeo2020.pl and
    # dc000012020.pl. Nothing here is downloaded; the bytes are the fixture.
    real_geo_050 = (
        "PLST|DC|050|00|00|000|00|0000002|0500000US11001|11001|3|5|11|0"
        "1702382|001|H6|01702382|||||||||||||||||||||||||||||||||47900|"
        "1|548|47894||||||||||||||||||||||||||||||||158316124|18709762|"
        "District of Columbia|District of "
        "Columbia|F||689545|350364|+38.9042474|-077.0165167|00||")
    real_geo_610 = (
        "PLST|DC|610|U6|00|000|00|0000998|610U600US11001|11001|3|5|11|0"
        "1702382||||||||||||||||||||||||||||||||||||||||||||||||||||||0"
        "01|||||||||||||||||6383120|182814|1|Ward "
        "1|N||85285|45694|+38.9252533|-077.0310006|L1||")
    real_seg1 = (
        "PLST|DC|000|01|0000002|689545|633468|273194|285810|3193|33545|"
        "432|37294|56077|51147|7543|2212|8646|247|23425|2334|1340|112|3"
        "856|70|20|767|190|300|85|4262|1379|394|23|792|88|3|781|153|273"
        "|15|63|39|127|32|49|27|2|0|4|18|613|129|8|354|14|51|1|1|21|3|1"
        "6|9|3|0|3|0|51|20|18|0|4|4|5|4|4|689545|77652|611893|582408|26"
        "1771|282066|1277|33192|349|3753|29485|26897|7099|1697|8414|196"
        "|3702|2189|1284|108|1751|62|18|41|168|165|3|2335|1115|350|20|3"
        "29|70|1|30|134|61|4|54|34|49|32|32|15|2|0|0|3|230|102|5|68|11|"
        "34|1|1|0|0|3|4|1|0|0|0|23|13|1|0|4|4|1|0|0")

    real = parse_geo_line(real_geo_050)
    check(real.sumlev == "050",
          "a real 2020 county geoheader record reads SUMLEV 050")
    check(real.logrecno == "0000002" and real.geocode == "11001",
          "its LOGRECNO is 0000002 and its GEOCODE is 11001")
    check(real.state == "11" and real.county == "001",
          "STATE 11 and COUNTY 001, with the eight digit STATENS 01702382 "
          "sitting between them  <-- pinned defect")
    off_by_one = GeoRow(real.sumlev, real.logrecno, real.geocode, real.state,
                        real_geo_050.split("|")[13], real.arealand,
                        real.areawatr)
    check(in_county(real, "001") and not in_county(off_by_one, "001"),
          "so a parser reading COUNTY one field past STATE takes 01702382, "
          "the GNIS code, and matches no county at all  <-- pinned defect")
    check((real.arealand, real.areawatr) == ("158316124", "18709762"),
          "and AREALAND and AREAWATR read as the published square metres")

    district = parse_geo_line(real_geo_610)
    check(district.geocode == real.geocode,
          "a real SUMLEV 610 state legislative district in the same file "
          "carries the SAME GEOCODE 11001 as the county  <-- pinned defect")
    check(prefix_only_match(district.geocode, "001")
          and not in_county(district, "001"),
          "so the prefix filter admits it and the COUNTY field does not: its "
          "COUNTY is empty  <-- pinned defect")
    check(in_county(real, "001"),
          "while the county record itself is admitted by both")

    real_logrec, real_counts = parse_seg1_line(real_seg1)
    check(real_logrec == "0000002",
          "the real segment 1 record joins to that geoheader row on LOGRECNO")
    check(real_counts.total == 689545,
          "its P0010001 is the published 2020 population of the District of "
          "Columbia")
    check(race_parts_sum(real_counts) == 689545,
          "and the seven P1 race columns of a REAL record add to it exactly, "
          "which no fixture this file wrote could have arranged")
    check(real_counts.one_race == 633468
          and race_parts_sum(real_counts) + real_counts.one_race == 1323013,
          "adding P0010002 to them gives 1323013 for a population of 689545  "
          "<-- pinned defect")
    check(real_counts.hispanic == 77652
          and real_counts.total + real_counts.hispanic == 767197,
          "and P1 total plus P2 Hispanic gives 767197 for the same 689545  "
          "<-- pinned defect")

    # ---- P0010002 is a subtotal, not an eighth part
    check(race_parts_sum(counts) == counts.total,
          "the seven P1 race columns add exactly to P1_001")
    check(race_parts_sum(counts) + counts.one_race != counts.total,
          "adding P0010002 to those seven counts the one-race population "
          "twice  <-- pinned defect")
    check("one_race" not in RACE_PARTS,
          "so P0010002 is not in RACE_PARTS at all")

    # ---- the county gate. Summary level first, then the prefix.
    check(prefix_only_match("12083", "083"),
          "the prefix-only filter admits GEOCODE 12083")
    check(prefix_only_match("120830001001001", "083"),
          "and it admits a block in county 083")
    check(not prefix_only_match("12081", "083"),
          "and it rejects the neighbouring county 081")
    check(not prefix_only_match("12", "083"),
          "and a GEOCODE shorter than five characters cannot match")
    by_logrec = dict((parse_geo_line(g).logrecno, parse_geo_line(g))
                     for g in geo_rows)
    sldl, place, block, control = (by_logrec["0000008"], by_logrec["0000009"],
                                   by_logrec["0000005"], by_logrec["0000002"])
    check(prefix_only_match(sldl.geocode, "083")
          and not in_county(sldl, "083"),
          "a SUMLEV 620 legislative district numbered 083 has GEOCODE 12083, "
          "passes the prefix test and is NOT in county 083  <-- pinned defect")
    check(sldl.county == "",
          "because its COUNTY field is empty, which is how the file says so")
    check(prefix_only_match(place.geocode, "083")
          and not in_county(place, "083"),
          "a SUMLEV 160 place with GEOCODE 1208300 passes the prefix test and "
          "is NOT in county 083  <-- pinned defect")
    check(in_county(block, "083"), "a block in county 083 is admitted")
    check(in_county(control, "083"),
          "the county control record is admitted")
    check(not in_county(by_logrec["0000011"], "083"),
          "a block of county 081 in the same state file is dropped")
    cross = by_logrec["0000012"]
    check(in_county(cross, "083")
          and not prefix_only_match(cross.geocode, "083"),
          "a SUMLEV 155 record IS in county 083 even though its GEOCODE is "
          "state + place + county and fails the prefix test  <-- pinned "
          "defect")
    check(not in_county(by_logrec["0000001"], "083"),
          "the state record is in no county at all")
    check(not in_county(by_logrec["0000001"], ""),
          "and an empty county FIPS matches nothing, rather than matching\n"
          "every record whose COUNTY field is also empty  <-- pinned defect")
    check(not in_county(sldl, ""),
          "which is the same record set the prefix filter would have "
          "called a county")
    check(by_logrec["0000005"].state == "12",
          "STATE is read at 12, two fields before COUNTY, not one  "
          "<-- pinned defect")

    # ---- the whole scan over the synthetic state file
    report = scan(geo_rows, seg_rows, "083")
    check(report.reconciled,
          "three blocks of 10, 12 and 8 reconcile to a county record of 30")
    check(report.extract_total == 30, "the extract sums to 30")
    check(report.control_total == 30,
          "the control total comes from the file's own SUMLEV 050 row")
    check(report.control_geoid == "12083",
          "and that row is named in the report")
    check(len(report.extract_rows) == 3, "three blocks are extracted")
    check([r[0] for r in report.extract_rows]
          == ["120830001001001", "120830001001002", "120830001001003"],
          "the extract is ordered by GEOID, so two runs over an unchanged "
          "file produce the same bytes")
    check([r[0] for r in report.extract_rows]
          != [by_logrec[lr].geocode
              for lr in ("0000005", "0000006", "0000007")],
          "and that is NOT the order the file lists them in, so the sort "
          "is doing work rather than agreeing with the input  "
          "<-- pinned defect")
    check(report.reasons == [], "a reconciled extract carries no reason")

    check(report.prefix_only_total == 127,
          "the same records filtered on the prefix alone sum to 127 for a "
          "county of 30  <-- pinned defect")
    check(report.prefix_only_total != report.control_total,
          "which reconciles against nothing, and is not a population")
    levels = dict((level.sumlev, level) for level in report.by_level)
    check(sorted(levels) == ["050", "140", "150", "155", "160", "620", "750"],
          "the level table shows every level either filter admits, the two "
          "that are not in the county included")
    check("040" not in levels,
          "the state record is in neither, its GEOCODE is too short to match "
          "and its COUNTY field is empty")
    check((levels["050"].p1_total, levels["140"].p1_total,
           levels["150"].p1_total, levels["750"].p1_total)
          == (30, 30, 30, 30),
          "county, tract, block group and blocks each carry the same 30 "
          "people")
    check((levels["620"].p1_total, levels["160"].p1_total) == (5, 2),
          "and the district and the place add 7 more that are not in the "
          "county")
    check(levels["620"].in_county is False
          and levels["620"].prefix_records == 1,
          "the table marks the district as not in the county, and as let in "
          "by the prefix")
    check(levels["155"].in_county is True
          and levels["155"].prefix_records == 0,
          "and marks the place-county record as in the county, and as one "
          "the prefix would have missed")
    check(levels["750"].records == 3 and levels["750"].in_county is True,
          "and the three blocks as extracted")

    # ---- P1 and P2 over the whole extract
    check(report.race_parts_total == 30,
          "the seven race columns over the extract add to exactly the "
          "population")
    check(report.hispanic_total == 7, "P2 Hispanic origin over it is 7")
    check(report.extract_total + report.hispanic_total == 37,
          "P1 total plus P2 Hispanic is 37 for a population of 30  "
          "<-- pinned defect")
    check("hispanic_any_race" in CSV_FIELDS and "hispanic" not in CSV_FIELDS,
          "so the extract names that column hispanic_any_race")
    check(len(CSV_FIELDS) == 12 and "one_race" not in CSV_FIELDS,
          "and the extract carries eight addable population columns, not "
          "nine and not ten")

    # ---- the neighbouring county, read out of the same file
    other = scan(geo_rows, seg_rows, "081")
    check(other.reconciled and other.extract_total == 99,
          "county 081 reconciles out of the same state file")
    check([r[0] for r in other.extract_rows] == ["120810001001001"],
          "and its extract holds only its own block")

    # ---- refusals
    missing = scan([g for g in geo_rows
                    if g.split("|")[2] != "050"],
                   seg_rows, "083")
    check(not missing.reconciled and missing.control_total is None,
          "a file with no SUMLEV 050 row for the county is REFUSED, not "
          "reported against nothing  <-- pinned defect")
    check(any("nothing in it to reconcile against" in r
              for r in missing.reasons),
          "and the refusal says the control record is absent")

    short = scan(geo_rows, without_seg("0000007"), "083")
    check(not short.reconciled and short.extract_total == 22,
          "an extract of 22 against a control of 30 is REFUSED")
    check(any("a difference of -8" in r for r in short.reasons),
          "and the refusal names the difference, not just the fact of one")

    # The other direction, which is the one this whole tool exists for: an
    # extract LARGER than the county record. A control record of 20 against
    # the same three blocks of 30.
    over = scan(geo_rows,
                without_seg("0000002")
                + [seg_line("0000002", 10, 4, 1, 1, 1, 1, 2, 5)],
                "083")
    check(not over.reconciled and over.extract_total == 30
          and over.control_total == 20,
          "an extract of 30 against a control of 20 is REFUSED")
    check(any("a difference of +10" in r for r in over.reasons),
          "and the difference is signed, so an over-count reads differently "
          "from an under-count of the same size  <-- pinned defect")

    dup = scan(geo_rows + [geo_line("050", "0000013", "12083", "083")],
               seg_rows + [seg_line("0000013", 16, 6, 1, 2, 1, 2, 2, 7)],
               "083")
    check(not dup.reconciled and any("ambiguous" in r for r in dup.reasons),
          "two SUMLEV 050 rows for one county are REFUSED as ambiguous, "
          "rather than one of them being picked")

    orphan = scan(geo_rows, without_seg("0000002"), "083")
    check(not orphan.reconciled
          and any("carries no population" in r for r in orphan.reasons),
          "a control record with no segment 1 row is REFUSED")

    lost = scan(geo_rows, without_seg("0000006"), "083")
    check(not lost.reconciled
          and any("have no segment 1 row" in r for r in lost.reasons),
          "a block with no segment 1 row is REFUSED, not dropped from a sum "
          "that then reconciles to nothing")

    corrupt = scan(geo_rows,
                   without_seg("0000005")
                   + [seg_line("0000005", 4, 2, 1, 1, 0, 0, 1, 3, total=10)],
                   "083")
    check(corrupt.extract_total == 30,
          "a block whose P1_001 still says 10 while its race columns add to "
          "9 leaves the county total looking right")
    check(not corrupt.reconciled
          and any("do not add to P1_001" in r for r in corrupt.reasons),
          "and it is REFUSED anyway, because the row disagrees with itself  "
          "<-- pinned defect")

    place = scan(geo_rows, seg_rows, "083", "160")
    check(not place.reconciled
          and any("no record in county 083 carries summary level 160" in r
                  for r in place.reasons),
          "asking for SUMLEV 160 out of a county is REFUSED: the place in "
          "this file is not in the county  <-- pinned defect")
    check(place.extract_rows == [], "and nothing is extracted for it")
    check(any("carries 050, 140, 150, 155, 750 for it" in r
              for r in place.reasons),
          "and the refusal names the levels the file DOES carry for that "
          "county, read off the file rather than off a list in this tool")

    itself = scan(geo_rows, seg_rows, "083", "050")
    check(not itself.reconciled
          and any("proves nothing" in r for r in itself.reasons),
          "extracting the control record and checking it against itself is "
          "REFUSED  <-- pinned defect")

    empty = scan(geo_rows, seg_rows, "083", "070")
    check(not empty.reconciled and empty.extract_total == 0,
          "a summary level the file does not carry at all extracts nothing "
          "and is REFUSED, rather than reconciling zero against zero")

    cross_only = scan(geo_rows, seg_rows, "083", "155")
    check(cross_only.reconciled and cross_only.extract_total == 30,
          "the SUMLEV 155 record the prefix filter would have missed "
          "reconciles as its own extract")

    bg = scan(geo_rows, seg_rows, "083", "150")
    check(bg.reconciled and bg.extract_total == 30,
          "the block group level reconciles too, so --sumlev is real")

    check(scan(geo_rows + ["", "   "], seg_rows + [""], "083").reconciled,
          "blank lines at the end of either file are skipped")

    # ---- two files joined together. LOGRECNO is the join key, so a repeat
    # silently replaces a record rather than adding one.
    raises(lambda: scan(geo_rows + [geo_line("750", "0000005",
                                             "120830009009009", "083")],
                        seg_rows, "083"),
           "a LOGRECNO repeated in the geoheader raises, rather than the "
           "second record replacing the first and a block leaving the "
           "extract without changing the row count  <-- pinned defect")
    raises(lambda: scan(geo_rows,
                        seg_rows + [seg_line("0000005", 99, 0, 0, 0, 0, 0, 0,
                                             0)],
                        "083"),
           "a LOGRECNO repeated in segment 1 raises, rather than the second "
           "population replacing the first  <-- pinned defect")
    check(scan(geo_rows,
               seg_rows + [seg_line("0009999", 99, 0, 0, 0, 0, 0, 0, 0)],
               "083").reconciled,
          "while a segment 1 row for a LOGRECNO this county does not carry "
          "is ignored, because the file holds the whole state")

    # ---- the two field-count checks are deliberately not the same check
    numbered = [str(i) for i in range(160)]
    check(parse_seg1_line("|".join(numbered[:150]))[1].total == 5,
          "segment 1 is checked as a MINIMUM of 149 fields, so a file with a "
          "column appended still reads: everything pl94 wants sits at 77 or "
          "below")
    check(parse_seg1_line("|".join(numbered[:149]))[1].total == 5,
          "and a record of exactly 149 reads, which is the boundary itself")
    raises(lambda: parse_seg1_line("|".join(numbered[:148])),
           "while 148 raises, so the minimum is 149 and not one less  "
           "<-- pinned defect")
    raises(lambda: parse_geo_line("|".join([""] * 98)),
          "while the geoheader is checked as an exact 97, because a record "
          "with the wrong count there is a different vintage, not a longer "
          "row  <-- pinned defect")

    raises(lambda: scan(geo_rows, seg_rows, "83"),
           "a two digit county FIPS is refused before anything is read")
    raises(lambda: scan(geo_rows, seg_rows, "abc"),
           "a county FIPS that is not digits is refused")
    raises(lambda: scan(geo_rows, seg_rows, "083", "75"),
           "a two digit summary level is refused")
    raises(lambda: scan(geo_rows, seg_rows, "083", "sev"),
           "a summary level that is not digits is refused")

    # ---- describe(), which is what an operator actually reads
    lines = describe(report, "state2020.pl.zip")
    check(lines[0] == "pl94: state2020.pl.zip",
          "the report names its source")
    check(any(l.startswith("  750") and "EXTRACTED" in l for l in lines),
          "the extracted level is marked in the level table")
    check(any(l.startswith("  620") and "NO, the GEOCODE prefix let it in" in l
              for l in lines),
          "the legislative district is marked as not in the county")
    check(any(l.startswith("  155") and "prefix misses 1 of them" in l
              for l in lines),
          "the place-county record is marked as one the prefix would miss")
    check(any(l.startswith("  050") and "control record" in l
              for l in lines),
          "the control record is marked as the control")
    check(any(l.startswith("  140") and "another cut of the same people" in l
              for l in lines),
          "the tract is marked as another cut of the same people")
    check(any("the GEOCODE prefix admits: 127" in l for l in lines),
          "the prefix-only sum is printed together with what it is not")
    check(any("Not a population" in l for l in lines),
          "and is labelled in words, not only by omission")
    check(any("P1_001 + P2 Hispanic              : 37" in l for l in lines),
          "the overlap of P1 and P2 is printed as the number it produces")
    check(any(l.startswith("RECONCILED") for l in lines),
          "a reconciled run says so on its own line")
    bad_lines = describe(short, "state2020.pl.zip")
    check(any(l.startswith("REFUSED") for l in bad_lines)
          and any("difference of -8" in l for l in bad_lines),
          "a refused run says so and lists its reasons")
    check(any("county control record             : none" in l
              for l in describe(missing, "x.zip")),
          "a run with no control record prints none rather than a zero")

    # ---- real files on disk. Everything below writes into one temporary
    # directory and deletes it again. No network, no credentials.
    tmp = tempfile.mkdtemp(prefix="pl94-selftest-")

    def make_zip(name, geo=None, seg=None, extra=None):
        path = os.path.join(tmp, name)
        with zipfile.ZipFile(path, "w") as zf:
            if geo is not None:
                zf.writestr("flgeo2020.pl",
                            "\r\n".join(geo).encode("latin-1"))
            if seg is not None:
                zf.writestr("fl000012020.pl",
                            "\r\n".join(seg).encode("latin-1"))
            for member, body in (extra or []):
                zf.writestr(member, body)
        return path

    def run_cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    # A real PL zip holds four members: the geoheader and segments 1, 2
    # and 3. Segment 2 is P3, P4 and H1; segment 3 is the group quarters
    # table. Both end in "2020.pl" and neither is what this tool reads.
    other_segments = [
        ("fl000022020.pl", b"PLST|FL|000|02|0000005|9|9|9\r\n"),
        ("fl000032020.pl", b"PLST|FL|000|03|0000005|9|9|9\r\n"),
    ]
    good_zip = make_zip("state2020.pl.zip", geo_rows, seg_rows,
                        other_segments)
    short_zip = make_zip("short.pl.zip", geo_rows, without_seg("0000007"))

    check(sorted(zipfile.ZipFile(good_zip).namelist())
          == ["fl000012020.pl", "fl000022020.pl", "fl000032020.pl",
              "flgeo2020.pl"],
          "the zip holds the four members a real PL download holds: the "
          "geoheader and segments 1, 2 and 3")

    code, out, err = run_cli(["--pl", good_zip, "--county", "083"])
    check(code == 0, "the real zip reconciles through the command line, so "
                     "segments 2 and 3 are passed over rather than read as "
                     "more segment 1  <-- pinned defect")
    check("RECONCILED" in out, "and says so")
    check("the GEOCODE prefix admits: 127" in out,
          "and prints the 127 it refused to call a population")
    check(err == "", "a clean run writes nothing to stderr")

    code, out, err = run_cli(["--pl", short_zip, "--county", "083"])
    check(code == 1, "a zip whose blocks do not reconcile exits 1")
    check("REFUSED" in out and "difference of -8" in out,
          "and prints the difference it found")

    # ---- the extract file itself
    dest = os.path.join(tmp, "blocks.csv")
    code, out, err = run_cli(["--pl", good_zip, "--county", "083",
                              "--out", dest])
    check(code == 0 and not os.path.exists(dest),
          "--out without --apply writes nothing  <-- pinned defect")
    check("was not written" in out, "and says the file was not written")

    code, out, err = run_cli(["--pl", good_zip, "--county", "083",
                              "--out", dest, "--apply"])
    check(code == 0 and os.path.exists(dest), "--apply writes the extract")
    with open(dest, "r") as fh:
        body = fh.read()
    csv_rows = list(csv.reader(body.splitlines()))
    check(csv_rows[0] == ["geoid", "arealand", "areawatr", "total",
                          "white", "black", "aian", "asian", "nhpi",
                          "other", "two_or_more", "hispanic_any_race"],
          "the header is these twelve columns in this order, named here "
          "rather than read back out of the constant that wrote them")
    check(len(csv_rows) == 4, "three block rows under it")
    check(all(len(r) == 12 for r in csv_rows),
          "and every row carries exactly twelve values, so a thirteenth "
          "column cannot ride in under a twelve column header  "
          "<-- pinned defect")
    check(csv_rows[1][0] == "120830001001001" and csv_rows[1][3] == "12",
          "the first row is the lowest GEOID with its own population, "
          "which is the block the file lists second")
    check(csv_rows[1][1] == "1000" and csv_rows[1][2] == "0",
          "AREALAND and AREAWATR are copied through as they were written")
    check(sum(int(r[3]) for r in csv_rows[1:]) == 30,
          "and the written rows still add up to the county control total")
    check("\r\n\r\n" not in body,
          "no blank line between rows, which is what newline='' prevents on "
          "windows  <-- pinned defect")

    refused = os.path.join(tmp, "refused.csv")
    code, out, err = run_cli(["--pl", short_zip, "--county", "083",
                              "--out", refused, "--apply"])
    check(code == 1 and not os.path.exists(refused),
          "--apply does NOT write an extract that failed to reconcile  "
          "<-- pinned defect")
    check("Refused" in out and "--allow-unreconciled" in out,
          "and the refusal names the flag that would override it")

    code, out, err = run_cli(["--pl", short_zip, "--county", "083",
                              "--out", refused, "--apply",
                              "--allow-unreconciled"])
    check(os.path.exists(refused), "--allow-unreconciled writes it anyway")
    check(code == 1,
          "and still exits 1, because the extract is still wrong  "
          "<-- pinned defect")

    bg_csv = os.path.join(tmp, "bg.csv")
    code, out, err = run_cli(["--pl", good_zip, "--county", "083",
                              "--sumlev", "150", "--out", bg_csv, "--apply"])
    check(code == 0, "--sumlev 150 extracts the block group and reconciles")
    with open(bg_csv, "r") as fh:
        check(len(fh.read().splitlines()) == 2,
              "and writes one block group row under the header")

    code, out, err = run_cli(["--pl", good_zip, "--county", "081"])
    check(code == 0 and "12081" in out,
          "the neighbouring county reconciles out of the same zip")

    # ---- unreadable input
    code, out, err = run_cli(["--pl", os.path.join(tmp, "nope.zip"),
                              "--county", "083"])
    check(code == 2 and "error:" in err, "a zip that is not there exits 2")

    not_a_zip = os.path.join(tmp, "plain.txt")
    with open(not_a_zip, "w") as fh:
        fh.write("this is not a zip file\n")
    code, out, err = run_cli(["--pl", not_a_zip, "--county", "083"])
    check(code == 2, "a file that is not a zip exits 2")

    no_geo = make_zip("nogeo.zip", None, seg_rows)
    code, out, err = run_cli(["--pl", no_geo, "--county", "083"])
    check(code == 2 and "geoheader" in err,
          "a zip with no geoheader file exits 2 and says which file is "
          "missing")

    no_seg = make_zip("noseg.zip", geo_rows, None)
    code, out, err = run_cli(["--pl", no_seg, "--county", "083"])
    check(code == 2 and "segment 1" in err,
          "a zip with no segment 1 file exits 2 and says so")

    two_geo = make_zip("twogeo.zip", geo_rows, seg_rows,
                       [("__MACOSX/flgeo2020.pl", b"junk")])
    code, out, err = run_cli(["--pl", two_geo, "--county", "083"])
    check(code == 2 and "look like the geoheader" in err,
          "two files that both look like the geoheader are REFUSED, not "
          "resolved by taking the first  <-- pinned defect")

    truncated = make_zip("trunc.zip", geo_rows,
                         seg_rows[:-1] + ["|".join(["0"] * 100)])
    code, out, err = run_cli(["--pl", truncated, "--county", "083"])
    check(code == 2 and "truncated" in err,
          "a truncated segment 1 record exits 2 rather than reading a short "
          "record as zeros")

    code, out, err = run_cli(["--pl", good_zip, "--county", "083",
                              "--out", tmp, "--apply"])
    check(code == 2 and "could not write" in err,
          "an extract that cannot be written exits 2, not 1, because 1 means "
          "the numbers disagreed")

    # ---- usage errors
    check(run_cli([])[0] == 64, "no arguments at all is a usage error")
    check(run_cli(["--county", "083"])[0] == 64, "--pl is required")
    check(run_cli(["--pl", good_zip])[0] == 64, "--county is required")
    check(run_cli(["--pl", good_zip, "--county", "83"])[0] == 64,
          "a two digit county is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "0083"])[0] == 64,
          "a four digit county is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "08x"])[0] == 64,
          "a county that is not digits is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "083",
                   "--sumlev", "75"])[0] == 64,
          "a two digit summary level is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "083", "--apply"])[0] == 64,
          "--apply without --out is a usage error, not a silent no-op")

    argv_before = sys.argv
    try:
        sys.argv = ["pl94.py"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main()
        check(code == 64,
              "main() called with no argv reads sys.argv and still refuses "
              "without --pl")
    finally:
        sys.argv = argv_before

    # ---- the command line itself
    args = _parse([])
    check(args.apply is False,
          "--apply is off by default, so nothing is ever written by accident")
    check(args.allow_unreconciled is False,
          "--allow-unreconciled is off by default  <-- pinned defect")
    check(args.self_test is False, "--self-test is off by default")
    check(args.county is None,
          "there is no default county, because a county baked into a tool is "
          "how it confidently reports the wrong place  <-- pinned defect")
    check(args.pl is None, "and no default input file")
    check(args.out is None, "and no default output path")
    check(args.sumlev == DEFAULT_SUMLEV,
          "the default summary level is blocks")
    check(_parse(["--apply"]).apply is True, "--apply is read")
    check(_parse(["--allow-unreconciled"]).allow_unreconciled is True,
          "--allow-unreconciled is read")
    check(_parse(["--self-test"]).self_test is True, "--self-test is read")
    check(_parse(["--pl", "a.zip"]).pl == "a.zip", "--pl is read")
    check(_parse(["--county", "083"]).county == "083", "--county is read")
    check(_parse(["--sumlev", "150"]).sumlev == "150", "--sumlev is read")
    check(_parse(["--out", "b.csv"]).out == "b.csv", "--out is read")
    check(not hasattr(_parse([]), "year"),
          "there is no --year, because the 2010 fixed-width layout was never "
          "round-tripped through a real 2010 file  <-- pinned defect")

    shutil.rmtree(tmp, ignore_errors=True)

    # ---- the harness itself, which has to be able to report red
    #
    # A self-test whose failure path is never exercised is not a control: it
    # reports green because nothing ever calls the other branch. The harness
    # is run here against three deliberate failures, with its output swallowed
    # and its tally put back, so a green run has still proven it can go red.
    def probe():
        check(False, "a false check must be recorded as a failure")
        raises(lambda: None, "a function that raises nothing must fail")
        raises(lambda: 1 / 0,
               "a function that raises the wrong thing must fail")

    kept_passed, kept_failed = passed[0], list(failed)
    noise = io.StringIO()
    with contextlib.redirect_stdout(noise):
        probe()
    probe_passed, probe_failed = passed[0], list(failed)
    passed[0], failed[:] = kept_passed, kept_failed
    check(len(probe_failed) - len(kept_failed) == 3,
          "the harness records a false check, a missing exception and a "
          "wrong exception as three failures, so a broken tool turns this "
          "self-test red  <-- pinned defect")
    check(probe_passed == kept_passed,
          "not one of those three was counted as a pass")
    check(noise.getvalue().count("FAIL  ") == 3,
          "and each one prints a FAIL line the operator can see")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _is_geo(basename):
    """The geoheader member of a 2020 PL zip, e.g. flgeo2020.pl."""
    return "geo" in basename and basename.endswith(".pl")


def _is_seg1(basename):
    """Segment 1 of a 2020 PL zip, e.g. fl000012020.pl."""
    return basename.endswith("012020.pl")


def _member(zf, predicate, what):
    """The one member matching predicate. Zero or several is a refusal.

    Taking the first match is the tempting version and it is wrong: a zip
    re-made on a Mac carries a __MACOSX copy of every file, and the first
    match is then a resource fork that parses as an empty geoheader.
    """
    hits = [name for name in zf.namelist()
            if predicate(os.path.basename(name).lower())]
    if not hits:
        raise MissingMember("no %s file in this zip" % what)
    if len(hits) > 1:
        raise MissingMember("%d files in this zip look like the %s: %s"
                            % (len(hits), what, ", ".join(sorted(hits))))
    return hits[0]


def read_pl(zip_path, county_fips, sumlev):
    """Reconcile one county out of a 2020 legacy-format PL zip."""
    with zipfile.ZipFile(zip_path) as zf:
        geo_name = _member(zf, _is_geo, "geoheader")
        seg_name = _member(zf, _is_seg1, "segment 1")
        with zf.open(geo_name) as geo_fh, zf.open(seg_name) as seg_fh:
            return scan(geo_fh, seg_fh, county_fips, sumlev)


def write_csv(path, rows):
    """Write the extract. newline='' or every row gains a blank line."""
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for geoid, geo, counts in rows:
            writer.writerow([geoid, geo.arealand, geo.areawatr, counts.total,
                             counts.white, counts.black, counts.aian,
                             counts.asian, counts.nhpi, counts.other,
                             counts.two, counts.hispanic])


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="pl94.py",
        description="Extract one county out of a Census PL 94-171 bulk file, "
                    "and refuse an extract that does not reconcile to the "
                    "county record in the same file.",
        epilog="Nothing is written without --apply, and an extract that does "
               "not reconcile is not written even then.",
    )
    ap.add_argument("--pl",
                    help="the 2020 PL 94-171 legacy-format state zip, as "
                         "downloaded from the Census Bureau")
    ap.add_argument("--county",
                    help="three digit county FIPS, e.g. 083. Required: there "
                         "is no default.")
    ap.add_argument("--sumlev", default=DEFAULT_SUMLEV,
                    help="three digit summary level to extract (default %s, "
                         "blocks)" % DEFAULT_SUMLEV)
    ap.add_argument("--out", help="path for the CSV extract")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--allow-unreconciled", dest="allow_unreconciled",
                    action="store_true",
                    help="write --out even when the extract does not "
                         "reconcile. The exit code still says it did not.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.pl:
        print("error: --pl is required. Use --self-test to verify the tool "
              "without a Census download.", file=sys.stderr)
        return 64
    if not args.county:
        print("error: --county is required, as three digits, e.g. 083.",
              file=sys.stderr)
        return 64
    if not (len(args.county) == 3 and args.county.isdigit()):
        print("error: --county must be three digits, got %r."
              % (args.county,), file=sys.stderr)
        return 64
    if not (len(args.sumlev) == 3 and args.sumlev.isdigit()):
        print("error: --sumlev must be three digits, got %r."
              % (args.sumlev,), file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out. There is nothing else to apply.",
              file=sys.stderr)
        return 64

    try:
        report = read_pl(args.pl, args.county, args.sumlev)
    except (IOError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    for line in describe(report, os.path.basename(args.pl)):
        print(line)

    if args.out:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        elif not report.reconciled and not args.allow_unreconciled:
            print("\nRefused. %s was NOT written, because the extract does "
                  "not reconcile to the county record in the same file.\n"
                  "Pass --allow-unreconciled if you need the rows anyway."
                  % args.out)
        else:
            try:
                write_csv(args.out, report.extract_rows)
            except (IOError, OSError, csv.Error) as exc:
                # Exit 2, not 1. A scheduled job reads 1 as "the numbers
                # disagreed", which is a fact about the data. A write that
                # failed is a fact about the disk and needs a different page.
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s (%d row(s))"
                  % (args.out, len(report.extract_rows)))

    return 0 if report.reconciled else 1


if __name__ == "__main__":
    sys.exit(main())
