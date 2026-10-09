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
gap is bulk and offline: the API documents a cap of 50 variables per call,
and its geography listing says a block query needs a state, a county and a
tract in its "in" clause, all against a live service. The bulk zip is one
download and is complete. This tool reads that zip
and does the one thing the file format will not do for you: it proves the
extract against the county control record carried in the same file, and
refuses to write an extract that does not match.

The same proof covers housing and area. H1 housing units, AREALAND and
AREAWATR over the extract must equal the county record too. It reads the
2020 pipe-delimited files and, with --year 2010, the 2010 fixed-width ones.

It is read-only against the PL file. The only thing it writes is a CSV
extract, and that needs --apply.

    python pl94.py --self-test
    python pl94.py --pl wy2020.pl.zip --county 021
    python pl94.py --pl wy2010.pl.zip --county 021 --year 2010
    python pl94.py --pl wy2020.pl.zip --county 021 --out blocks.csv --apply

Exit codes: 0 the extract reconciles, 1 it does not (the CSV is written only
with --allow-unreconciled), 2 the file could not be read, the CSV could not
be written, the report could not be written to stdout or argparse rejected
an argument, 64 usage error found by pl94.
"""

from __future__ import print_function

import argparse
import collections
import csv
import os
import sys
import zipfile
import zlib

# zipfile imports lzma itself and keeps None when the build lacks it. zlib.error
# stands in then: it is caught anyway, and no LZMA member can be read at all.
LZMA_ERROR = getattr(zipfile.lzma, "LZMAError", zlib.error)

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

# AREALAND and AREAWATR sit near the end of the 97 fields, in square metres.
# They are read as TEXT and copied to the extract as written. They are also
# summed over the extract and compared with the county record, because block
# areas tile the county exactly: every county in the 2010 and 2020 Wyoming
# files and the 2020 District of Columbia file matches to the square metre.
GEO_AREALAND = 84
GEO_AREAWATR = 85

# The 2010 geoheader is FIXED WIDTH: 500 characters and no delimiter at all.
# These are [start, end) slices, each the 1-based starting position and field
# size in Figure 2-4 of the 2010 technical documentation, PL/10-2 (RV).
# Numbers are right-aligned and padded with SPACES, not zeros, so every field
# is stripped as it is cut. There is no GEOCODE field in 2010.
GEO2010_LENGTH = 500
GEO2010 = {
    "sumlev": (8, 11), "logrecno": (18, 25), "state": (27, 29),
    "county": (29, 32), "cousub": (36, 41), "place": (45, 50),
    "tract": (54, 60), "blkgrp": (60, 61), "block": (61, 65),
    "concit": (67, 72), "cd": (153, 155), "sldu": (155, 158),
    "sldl": (158, 161), "vtd": (161, 167), "submcd": (176, 181),
    "sdelm": (183, 188), "sdsec": (188, 193), "sduni": (193, 198),
    "arealand": (198, 212), "areawatr": (212, 226),
}

# So the 2010 key is COMPOSED, per summary level, from the concatenations in
# the Census Bureau's "GEOID Construction for Matching" note for the 2010 PL
# file. 040 and 050 are not in that note and are the obvious two. This is a
# SUBSET of the note: its rules for the Alaska Native Regional Corporation
# level 230 and the American Indian area levels 281, 283 and 286, and its
# dissolve rule for 280, are not here. Those records
# carry a blank COUNTY, so no county extract can reach them. A level that is
# not here composes no key, and pl94 refuses to extract it rather than invent
# one. The 2020 GEOCODE for each level here is the same string.
GEOID2010 = {
    "040": ("state",),
    "050": ("state", "county"),
    "060": ("state", "county", "cousub"),
    "067": ("state", "county", "cousub", "submcd"),
    "140": ("state", "county", "tract"),
    "150": ("state", "county", "tract", "blkgrp"),
    "160": ("state", "place"),
    "170": ("state", "concit"),
    "500": ("state", "cd"),
    "610": ("state", "sldu"),
    "620": ("state", "sldl"),
    "700": ("state", "county", "vtd"),
    "750": ("state", "county", "tract", "block"),
    "950": ("state", "sdelm"),
    "960": ("state", "sdsec"),
    "970": ("state", "sduni"),
}

# Segments 1 and 2 have the same shape in both years. Only the delimiter
# differs: a pipe in 2020, a comma in 2010.
SEG_SEPS = {"2020": "|", "2010": ","}
YEARS = tuple(sorted(SEG_SEPS))
DEFAULT_YEAR = "2020"

# What the key is called in the level table. 2020 carries GEOCODE; 2010 does
# not, so the table names the composed GEOID instead.
KEY_NAMES = {"2020": "GEOCODE", "2010": "composed GEOID"}

# Segment 1 is pipe delimited with 149 fields: 5 header fields, then table P1
# (71 fields) and table P2 (73 fields). A shorter record is a truncated file.
SEG_SEP = "|"
SEG1_FIELDS = 5 + 71 + 73
SEG1_LOGRECNO = 4

# Segment 2 is 5 header fields, P3 (71), P4 (73), then H1 (3): total housing
# units, occupied and vacant. Occupied plus vacant is the total, exactly.
SEG2_FIELDS = 5 + 71 + 73 + 3
H1_TOTAL, H1_OCCUPIED, H1_VACANT = 149, 150, 151

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
              "hispanic_any_race", "housing_units", "occupied", "vacant")

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

Housing = collections.namedtuple("Housing", "total occupied vacant")

Report = collections.namedtuple("Report",
                                "county sumlev control_geoid control_total "
                                "extract_total extract_rows by_level "
                                "prefix_only_total one_race_total "
                                "race_parts_total hispanic_total bad_race "
                                "reasons reconciled year control_hu "
                                "extract_hu control_land extract_land "
                                "control_water extract_water bad_housing")

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
    """One record as text. PL files are latin-1, and lines end LF or CRLF."""
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


def parse_geo_line_2010(line):
    """Return a GeoRow from one 2010 fixed-width geoheader record."""
    if len(line) != GEO2010_LENGTH or GEO_SEP in line:
        # Both, not either. A 2020 record is usually far shorter than 500,
        # but one that happens to be exactly 500 still carries its pipes.
        raise MalformedRecord(
            "geoheader record is %d characters, the 2010 layout is %d with no "
            "delimiter" % (len(line), GEO2010_LENGTH))

    def cut(name):
        start, end = GEO2010[name]
        return line[start:end].strip()

    sumlev = cut("sumlev")
    geocode = "".join(cut(name) for name in GEOID2010.get(sumlev, ()))
    return GeoRow(sumlev, cut("logrecno"), geocode, cut("state"),
                  cut("county"), cut("arealand"), cut("areawatr"))


def parse_seg1_line(line, sep=SEG_SEP):
    """Return (logrecno, Counts) from one segment 1 record."""
    f = line.split(sep)
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


def parse_seg2_line(line, sep=SEG_SEP):
    """Return (logrecno, Housing) from one segment 2 record: table H1."""
    f = line.split(sep)
    if len(f) < SEG2_FIELDS:
        raise MalformedRecord(
            "segment 2 record has %d fields, expected %d. The file is "
            "truncated." % (len(f), SEG2_FIELDS))
    try:
        values = [int(f[i]) for i in (H1_TOTAL, H1_OCCUPIED, H1_VACANT)]
    except ValueError:
        raise MalformedRecord(
            "segment 2 record %r carries a housing count that is not a number"
            % (f[SEG1_LOGRECNO],))
    return f[SEG1_LOGRECNO].strip(), Housing(*values)


# The geoheader parser for each year. Defined here, after both parsers.
GEO_PARSERS = {"2020": parse_geo_line, "2010": parse_geo_line_2010}


def _three_digits(text):
    """True for exactly three ASCII digits.

    Not str.isdigit() alone: that accepts Arabic-Indic and other Unicode
    digits, which then match no county and crash the report on a console
    that cannot print them.
    """
    return len(text) == 3 and all(c in "0123456789" for c in text)


def _whole(text, what, geoid):
    """An area field as an integer, or a refusal to guess one."""
    if not text.isdigit():
        raise MalformedRecord("%s of %s is %r, not a whole number of square "
                              "metres" % (what, geoid, text))
    return int(text)


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
    that is laid out differently for each of the 40 to 66 levels in a file.
    """
    return bool(county_fips) and geo.county == county_fips


def race_parts_sum(counts):
    """The seven P1 columns that add up to P1_001. Never eight."""
    return sum(getattr(counts, name) for name in RACE_PARTS)


# ---------------------------------------------------------------- the report

def build_report(loose, counts, housing, county_fips, sumlev,
                 year=DEFAULT_YEAR):
    """Reconcile an extract against the county control record.

    loose   -- {logrecno: GeoRow} for every record EITHER filter let through,
               so the level table can show what each of them admits.
    counts  -- {logrecno: Counts} for those of them that have a segment 1 row.
    housing -- {logrecno: Housing} for those of them that have a segment 2 row.
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
    control_geoid, control_total, control_hu = None, None, None
    control_land, control_water = None, None
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
        control_geo = controls[0][0]
        control_geoid = control_geo.geocode
        control_land = _whole(control_geo.arealand, "AREALAND", control_geoid)
        control_water = _whole(control_geo.areawatr, "AREAWATR",
                               control_geoid)
        if controls[0][1] is None:
            reasons.append(
                "the county control record %s has no segment 1 row, so it "
                "carries no population" % control_geoid)
        else:
            control_total = controls[0][1].total
        logrec = control_geo.logrecno
        if logrec not in housing:
            reasons.append(
                "the county control record %s has no segment 2 row, so it "
                "carries no housing count" % control_geoid)
        else:
            control_hu = housing[logrec].total

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
    unhoused = sorted(geoid for geoid, _, logrec in wanted
                      if logrec not in housing)
    if unhoused:
        reasons.append(
            "%d summary level %s record(s) have no segment 2 row, so their "
            "housing count is unknown: %s"
            % (len(unhoused), sumlev, ", ".join(unhoused[:3])))
    keyless = len([1 for geoid, _, _ in wanted if not geoid])
    if keyless:
        # Only the 2010 layout can get here: it has no GEOCODE, and pl94
        # composes one only for the levels in GEOID2010.
        reasons.append(
            "%d summary level %s record(s) have no GEOID, because pl94 knows "
            "no %s rule to compose one for that level" % (keyless, sumlev,
                                                          year))

    extract_rows = sorted(((geoid, geo, counts[logrec], housing[logrec])
                           for geoid, geo, logrec in wanted
                           if logrec in counts and logrec in housing),
                          key=lambda row: row[0])

    extract_total = sum(row[2].total for row in extract_rows)
    one_race_total = sum(row[2].one_race for row in extract_rows)
    race_parts_total = sum(race_parts_sum(row[2]) for row in extract_rows)
    hispanic_total = sum(row[2].hispanic for row in extract_rows)
    extract_hu = sum(row[3].total for row in extract_rows)
    extract_land = sum(_whole(row[1].arealand, "AREALAND", row[0])
                       for row in extract_rows)
    extract_water = sum(_whole(row[1].areawatr, "AREAWATR", row[0])
                        for row in extract_rows)

    bad_race = [row[0] for row in extract_rows
                if race_parts_sum(row[2]) != row[2].total]
    if bad_race:
        reasons.append(
            "%d record(s) whose seven P1 race columns do not add to P1_001: "
            "%s" % (len(bad_race), ", ".join(bad_race[:3])))

    bad_housing = [row[0] for row in extract_rows
                   if row[3].occupied + row[3].vacant != row[3].total]
    if bad_housing:
        reasons.append(
            "%d record(s) whose H1 occupied and vacant do not add to the "
            "housing total: %s" % (len(bad_housing),
                                   ", ".join(bad_housing[:3])))

    for what, got, want in (("P1_001 population", extract_total,
                              control_total),
                             ("H1 housing units", extract_hu, control_hu),
                             ("AREALAND", extract_land, control_land),
                             ("AREAWATR", extract_water, control_water)):
        if want is not None and got != want:
            reasons.append(
                "%s over summary level %s sums to %d, the county control "
                "record %s says %d, a difference of %+d"
                % (what, sumlev, got, control_geoid, want, got - want))

    return Report(county=county_fips, sumlev=sumlev,
                  control_geoid=control_geoid, control_total=control_total,
                  extract_total=extract_total, extract_rows=extract_rows,
                  by_level=by_level, prefix_only_total=prefix_only_total,
                  one_race_total=one_race_total,
                  race_parts_total=race_parts_total,
                  hispanic_total=hispanic_total, bad_race=bad_race,
                  reasons=reasons, reconciled=not reasons, year=year,
                  control_hu=control_hu, extract_hu=extract_hu,
                  control_land=control_land, extract_land=extract_land,
                  control_water=control_water, extract_water=extract_water,
                  bad_housing=bad_housing)


def _segment(lines, parse, sep, loose, name):
    """{logrecno: row} out of one segment, for the records loose holds."""
    rows = {}
    for raw in lines:
        line = _line(raw)
        if not line.strip():
            continue
        logrec, row = parse(line, sep)
        if logrec in loose:
            if logrec in rows:
                # The same reason as in the geoheader, and worse: the count
                # itself is the thing being replaced.
                raise MalformedRecord(
                    "LOGRECNO %s appears twice in %s, carrying %d and then "
                    "%d. Two files have been joined together."
                    % (logrec, name, rows[logrec].total, row.total))
            rows[logrec] = row
    return rows


def scan(geo_lines, seg_lines, seg2_lines, county_fips,
         sumlev=DEFAULT_SUMLEV, year=DEFAULT_YEAR):
    """Read a geoheader and segments 1 and 2 and reconcile one county."""
    if year not in GEO_PARSERS:
        raise ValueError("year must be one of %s, got %r"
                         % (", ".join(YEARS), year))
    if not _three_digits(county_fips):
        raise ValueError("county FIPS must be three digits, got %r"
                         % (county_fips,))
    if not _three_digits(sumlev):
        raise ValueError("summary level must be three digits, got %r"
                         % (sumlev,))

    parse_geo, sep = GEO_PARSERS[year], SEG_SEPS[year]
    loose = {}
    for raw in geo_lines:
        line = _line(raw)
        if not line.strip():
            continue
        geo = parse_geo(line)
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

    counts = _segment(seg_lines, parse_seg1_line, sep, loose, "segment 1")
    housing = _segment(seg2_lines, parse_seg2_line, sep, loose, "segment 2")
    return build_report(loose, counts, housing, county_fips, sumlev, year)


def describe(report, source):
    """The report as printable lines."""
    key = KEY_NAMES[report.year]
    out = ["pl94: %s" % source,
           "  county %s, summary level %s, %s layout"
           % (report.county, report.sumlev, report.year),
           "",
           "  %-8s%10s%14s   %s"
           % ("SUMLEV", "records", "sum P1_001", "in county %s?"
              % report.county)]
    for level in report.by_level:
        if not level.in_county:
            note = "NO, the %s prefix let it in" % key
        elif report.year == "2010" and level.sumlev not in GEOID2010:
            # No key, so a prefix "miss" here would be pl94's missing rule
            # speaking, not a measurement of the prefix filter.
            note = "yes, and pl94 has no 2010 GEOID rule for it"
        elif level.prefix_records < level.records:
            note = ("yes, and the %s prefix misses %d of them"
                    % (key, level.records - level.prefix_records))
        elif level.sumlev == report.sumlev:
            note = "yes, EXTRACTED"
        elif level.sumlev == CONTROL_SUMLEV:
            note = "yes, the county control record"
        else:
            note = "yes, another cut of the same people"
        out.append("  %-8s%10d%14d   %s"
                   % (level.sumlev, level.records, level.p1_total, note))

    out.append("")
    out.append("  sum over every level the %s prefix admits: %d"
               % (key, report.prefix_only_total))
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
    for what, got, want in (("H1 housing units", report.extract_hu,
                             report.control_hu),
                            ("AREALAND sq m", report.extract_land,
                             report.control_land),
                            ("AREAWATR sq m", report.extract_water,
                             report.control_water)):
        out.append("  %-17s extract / county : %d / %s"
                   % (what, got, "none" if want is None else want))
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
    import atexit
    import contextlib
    import hashlib
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
        f[0], f[1] = "PLST", "ZZ"
        f[2] = sumlev
        f[7] = logrec
        f[9] = geocode
        f[12] = "99"
        f[13] = "00009999"          # STATENS, the field that is NOT the county
        f[14] = county
        f[84] = land
        f[85] = water
        return "|".join(f)

    def seg_line(logrec, white, black, aian, asian, nhpi, other, two, hisp,
                 total=None):
        one = white + black + aian + asian + nhpi + other
        f = ["0"] * 149
        f[0], f[1] = "PLST", "ZZ"
        f[4] = logrec
        f[5] = str(one + two if total is None else total)
        f[6] = str(one)
        for pos, value in ((7, white), (8, black), (9, aian), (10, asian),
                           (11, nhpi), (12, other), (13, two)):
            f[pos] = str(value)
        f[77] = str(hisp)
        return "|".join(f)

    def seg2_line(logrec, occupied, vacant, total=None):
        f = ["0"] * 152
        f[0], f[1], f[3] = "PLST", "ZZ", "02"
        f[4] = logrec
        f[149] = str(occupied + vacant if total is None else total)
        f[150] = str(occupied)
        f[151] = str(vacant)
        return "|".join(f)

    # One synthetic state file, laid out like the real ones. County 045 holds
    # three blocks of 10, 12 and 8. Every level over them repeats the same 30
    # people. Two levels that are not in the county at all carry 045 where a
    # county FIPS would sit, and one level that IS in the county does not.
    # The blocks hold 1000 square metres of land each, and the second of them
    # 7 of water, so every level over them carries 3000 and 7.
    geo_rows = [
        geo_line("040", "0000001", "99"),                      # state
        geo_line("050", "0000002", "99045", "045", "3000", "7"),  # control
        geo_line("140", "0000003", "99045000100", "045", "3000", "7"),
        geo_line("150", "0000004", "990450001001", "045", "3000", "7"),
        # Listed OUT of GEOID order on purpose. Three blocks written in
        # ascending order would sort into the order they were read, and
        # an assertion about sorting would pass with no sort at all.
        geo_line("750", "0000005", "990450001001003", "045"),  # block
        geo_line("750", "0000006", "990450001001001", "045"),  # block
        geo_line("750", "0000007", "990450001001002", "045", "1000", "7"),
        geo_line("620", "0000008", "99045"),                   # SLDL 045
        geo_line("160", "0000009", "9904500"),                 # place 04500
        geo_line("155", "0000012", "9954000045", "045", "3000", "7"),
        geo_line("050", "0000010", "99047", "047"),            # next county
        geo_line("750", "0000011", "990470001001001", "047"),  # its block
    ]
    # H1 for the same records: blocks of 4, 5 and 3 housing units under a
    # county of 12.
    seg2_rows = [
        seg2_line("0000001", 70, 10),                       # state, 80
        seg2_line("0000002", 10, 2),                        # control, 12
        seg2_line("0000003", 10, 2),                        # tract, 12
        seg2_line("0000004", 10, 2),                        # block group, 12
        seg2_line("0000005", 3, 1),                         # block, 4
        seg2_line("0000006", 5, 0),                         # block, 5
        seg2_line("0000007", 2, 1),                         # block, 3
        seg2_line("0000008", 2, 0),                         # SLDL 045, 2
        seg2_line("0000009", 1, 0),                         # place, 1
        seg2_line("0000012", 10, 2),                        # place-county, 12
        seg2_line("0000010", 35, 5),                        # next county, 40
        seg2_line("0000011", 35, 5),                        # its block, 40
    ]

    def without_seg2(logrec):
        return [s for s in seg2_rows if s.split("|")[4] != logrec]
    seg_rows = [
        seg_line("0000001", 120, 40, 4, 16, 2, 8, 10, 30),  # state, 200
        seg_line("0000002", 16, 6, 1, 2, 1, 2, 2, 7),       # control, 30
        seg_line("0000003", 16, 6, 1, 2, 1, 2, 2, 7),       # tract, 30
        seg_line("0000004", 16, 6, 1, 2, 1, 2, 2, 7),       # block group, 30
        seg_line("0000005", 5, 2, 1, 1, 0, 0, 1, 3),        # block, 10
        seg_line("0000006", 7, 3, 0, 1, 1, 0, 0, 2),        # block, 12
        seg_line("0000007", 4, 1, 0, 0, 0, 2, 1, 2),        # block, 8
        seg_line("0000008", 3, 1, 0, 0, 0, 1, 0, 1),        # SLDL 045, 5
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
    check(row.geocode == "990450001001003", "its GEOCODE is read at 9")
    check((row.arealand, row.areawatr) == ("1000", "0"),
          "AREALAND and AREAWATR are read as text, so the extract copies "
          "them as written and only the reconcile turns them into numbers")
    padded = geo_rows[4].split("|")
    padded[7], padded[14] = "0000005 ", " 045"
    padded = parse_geo_line("|".join(padded))
    check((padded.logrecno, padded.county) == ("0000005", "045"),
          "a LOGRECNO or COUNTY padded with a space is read stripped, so it "
          "still joins and still matches its county")
    raises(lambda: parse_geo_line("12|045"),
           "a geoheader record with 2 fields instead of 97 raises")
    raises(lambda: parse_geo_line(geo_rows[4] + "|extra"),
           "a geoheader record with 98 fields raises, because the layout is "
           "exact")
    raises(lambda: parse_geo_line("PLST ZZ750  0000005990450001001001"),
           "a fixed-width record with no pipes raises rather than reading as "
           "97 empty fields  <-- pinned defect")

    logrec, counts = parse_seg1_line(seg_rows[4])
    check(logrec == "0000005", "a segment 1 record's LOGRECNO is read at 4")
    check(parse_seg1_line(seg_rows[4].replace("|0000005|", "|0000005 |"))[0]
          == "0000005",
          "and read stripped, so a padded one still joins the geoheader")
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

    logrec, homes = parse_seg2_line(seg2_rows[4])
    check(logrec == "0000005", "a segment 2 record's LOGRECNO is read at 4")
    check(homes == (4, 3, 1),
          "H0010001 to H0010003 are read at 149, 150 and 151, after P3 and "
          "P4")
    raises(lambda: parse_seg2_line("|".join(["0"] * 151)),
           "a segment 2 record of 151 fields raises, so H1 is never read out "
           "of a truncated row  <-- pinned defect")
    check(parse_seg2_line("|".join(["0"] * 152))[1] == (0, 0, 0),
          "while 152 reads, which is the boundary itself")
    broken = seg2_rows[4].split("|")
    broken[150] = "x"
    raises(lambda: parse_seg2_line("|".join(broken)),
           "a segment 2 record with a housing count that is not a number "
           "raises")
    broken = seg2_rows[4].split("|")
    broken[149] = ""
    raises(lambda: parse_seg2_line("|".join(broken)),
           "and so does an empty H1 total, rather than reading it as zero")
    check(parse_seg2_line(seg2_rows[4].replace("|", ","), ",")[1] == homes,
          "and the same record comma delimited reads the same, given the "
          "comma")
    check(parse_seg1_line(seg_rows[4].replace("|", ","), ",")[1] == counts,
          "as does a comma delimited segment 1 record")
    raises(lambda: parse_seg1_line(seg_rows[4].replace("|", ",")),
           "while a comma delimited record read with the 2020 pipe raises as "
           "truncated, rather than reading as one field  <-- pinned defect")

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

    # Segment 2 of the same file, LOGRECNO 0000002, member dc000022020.pl.
    real_seg2 = (
        "PLST|DC|000|02|0000002|575161|532702|244170|226137|2554|31093|3"
        "69|28379|42459|38841|5267|1835|5490|204|18961|1821|943|91|3097|"
        "61|16|599|164|227|65|3122|1044|245|18|594|51|2|659|104|148|13|4"
        "7|29|89|21|28|15|1|0|0|14|459|88|7|286|10|31|0|0|13|3|15|5|1|0|"
        "0|0|37|16|17|0|0|4|0|0|0|575161|60375|514786|493346|234767|2235"
        "40|1063|30795|305|2876|21440|19540|4997|1462|5362|170|2979|1730"
        "|909|87|1457|53|14|33|149|135|3|1722|870|211|15|259|45|0|25|94|"
        "46|4|40|24|36|21|25|3|1|0|0|3|164|70|5|51|10|22|0|0|0|0|2|4|0|0"
        "|0|0|14|9|1|0|0|4|0|0|0|350364|312448|37916")
    real_logrec2, real_homes = parse_seg2_line(real_seg2)
    check(real_logrec2 == "0000002" and real_homes.total == 350364,
          "the real 2020 segment 2 record carries H0010001 350364, the same "
          "number as HU100 in the real geoheader record above")
    check(real_homes.occupied + real_homes.vacant == real_homes.total,
          "and its occupied 312448 and vacant 37916 add to it exactly")

    # ---- four real 2010 records, out of the published 2010 PL 94-171 file
    # for Wyoming: wy2010.pl.zip, members wygeo2010.pl, wy000012010.pl and
    # wy000022010.pl. The two geoheader records are 500 characters, mostly
    # spaces, so they are written as runs of text and runs of spaces, and the
    # SHA-1 of each published line pins the reassembly to the real bytes.
    wy_geo_050 = (
        "PLST  WY05000000  00469554856021H1" + " " * 78 + "16940  99999999"
        + " " * 75 + "6956479515" + " " * 7 + "4269807Laramie County"
        + " " * 76 + "A!" + " " * 4 + "91738" + " " * 4
        + "40462+41.2928302-104.660395006" + " " * 12 + "0177980701605075"
        + " " * 86 + "1" + " " * 24)
    wy_geo_610 = (
        "PLST  WY61000000  00014874856" + " " * 126 + "021" + " " * 44
        + "2872972033" + " " * 7 + "4801866State Senate District 21"
        + " " * 66 + "N" + " " * 5 + "18170" + " " * 5
        + "8748+44.7886414-107.3796141LU" + " " * 12 + "01779807"
        + " " * 119)
    wy_seg1 = (
        "PLST,WY,000,01,0046955,91738,88927,81205,2248,878,976,142,3478,"
        "2811,2628,649,676,469,56,514,41,36,4,61,16,5,41,19,33,8,166,55,"
        "27,1,7,15,8,15,18,3,5,0,1,5,0,1,0,1,0,0,4,15,8,0,3,0,2,1,0,0,0,"
        "1,0,0,0,0,0,1,1,0,0,0,0,0,1,1,91738,11978,79760,77947,74120,206"
        "3,595,935,119,115,1813,1710,557,535,412,46,52,26,32,4,5,9,4,3,1"
        "8,6,1,97,46,17,1,1,4,7,1,15,0,0,0,1,2,0,1,0,1,0,0,0,6,6,0,0,0,0"
        ",0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0")
    wy_seg2 = (
        "PLST,WY,000,02,0046955,69337,68005,62446,1654,651,778,105,2371,"
        "1332,1255,188,426,206,26,248,24,23,3,29,10,3,25,18,21,5,68,32,2"
        ",1,3,4,2,7,7,1,1,0,1,4,0,1,0,1,0,0,1,7,4,0,1,0,0,1,0,0,0,1,0,0,"
        "0,0,0,1,1,0,0,0,0,0,1,1,69337,7717,61620,60720,57800,1565,452,7"
        "46,89,68,900,849,165,362,188,22,33,18,20,3,4,5,3,3,17,5,1,47,27"
        ",2,1,1,2,2,1,6,0,0,0,1,2,0,1,0,1,0,0,0,4,4,0,0,0,0,0,0,0,0,0,0,"
        "0,0,0,0,0,0,0,0,0,0,0,0,0,40462,37576,2886")
    check((hashlib.sha1(wy_geo_050.encode("ascii")).hexdigest(),
           hashlib.sha1(wy_geo_610.encode("ascii")).hexdigest())
          == ("dd4d9b8a762ecb3bb3318d98b452ae7f29ea91a0",
              "8eecde57c38efee730c5487a291bbd5e4e938128"),
          "the two 2010 geoheader records reassemble to the exact bytes of "
          "the published lines")
    check(len(wy_geo_050) == 500 and len(wy_geo_610) == 500,
          "and each is 500 characters, the 2010 fixed-width record length")

    wy = parse_geo_line_2010(wy_geo_050)
    check((wy.sumlev, wy.logrecno, wy.state, wy.county)
          == ("050", "0046955", "56", "021"),
          "a real 2010 county record reads SUMLEV 050, LOGRECNO 0046955, "
          "STATE 56 and COUNTY 021 at Figure 2-4's positions")
    check(wy.geocode == "56021",
          "its key is composed as STATE + COUNTY, 56021, because 2010 has no "
          "GEOCODE field")
    check((wy.arealand, wy.areawatr) == ("6956479515", "4269807"),
          "AREALAND and AREAWATR come out of their space-padded 14 character "
          "fields as the published square metres  <-- pinned defect")
    raises(lambda: parse_geo_line(wy_geo_050),
           "the 2020 parser refuses the real 2010 record outright")
    raises(lambda: parse_geo_line_2010(real_geo_050),
           "and the 2010 parser refuses the real 2020 record")
    raises(lambda: parse_geo_line_2010(wy_geo_050[:499]),
           "a 2010 record one character short raises, because the layout is "
           "exact")
    # A name re-encoded as UTF-8 makes the record one BYTE longer. Read as
    # latin-1 that is 501 characters; read as UTF-8 it would be 500 again.
    utf8_record = (wy_geo_050[:226] + u"\xf1" + wy_geo_050[227:]).encode(
        "utf-8")
    raises(lambda: parse_geo_line_2010(_line(utf8_record)),
           "and a 2010 record re-encoded as UTF-8, with n with a tilde in "
           "two bytes, reads as 501 latin-1 characters and raises too  "
           "<-- pinned defect")
    raises(lambda: parse_geo_line_2010(wy_geo_050[:-1] + "|"),
           "and so does a record of exactly 500 characters that carries a "
           "pipe, which is a 2020 record of unlucky length  <-- pinned defect")

    senate = parse_geo_line_2010(wy_geo_610)
    check(senate.geocode == wy.geocode == "56021",
          "a real 2010 SUMLEV 610 senate district 21 composes the SAME GEOID "
          "56021 as the county, the 2010 twin of the DC case  <-- pinned "
          "defect")
    check(prefix_only_match(senate.geocode, "021")
          and not in_county(senate, "021") and senate.county == "",
          "so the prefix admits it and the COUNTY field, blank in the record, "
          "does not")

    # Every field the 2010 key rules read, each filled to its full width at
    # the 1-based position in Figure 2-4 and in the Bureau's SAS import
    # program, pl_all_3_2010.sas. A full-width value is what catches a slice
    # that starts one character late: a short, right-aligned value would
    # still read whole through it. BLKGRP is 2 under a block that starts
    # with 1, so a BLKGRP slice one late reads the block's first digit and
    # fails, where a real file, whose block numbers start with their block
    # group, would hide it.
    full = [" "] * 500
    for pos, text in ((19, "0000042"), (28, "99"), (30, "045"),
                      (37, "12345"), (46, "23456"), (55, "000100"),
                      (61, "2"), (62, "1001"), (68, "34567"), (154, "07"),
                      (156, "021"), (159, "045"), (162, "ABC123"),
                      (177, "45678"), (184, "56789"), (189, "67890"),
                      (194, "78901"), (199, "12345678901234"),
                      (213, "98765432109876")):
        full[pos - 1:pos - 1 + len(text)] = list(text)

    def composed(sumlev):
        full[8:11] = list(sumlev)
        return parse_geo_line_2010("".join(full)).geocode
    rules = {"040": "99", "050": "99045", "060": "9904512345",
             "067": "990451234545678", "140": "99045000100",
             "150": "990450001002", "160": "9923456", "170": "9934567",
             "500": "9907", "610": "99021", "620": "99045",
             "700": "99045ABC123", "750": "990450001001001",
             "950": "9956789", "960": "9967890", "970": "9978901"}
    check(dict((lv, composed(lv)) for lv in rules) == rules,
          "the fourteen 2010 GEOID rules the construction note prints, "
          "plus pl94's own 040 and 050, compose their concatenations out of "
          "full-width fields at the published positions  <-- pinned defect")
    check(sorted(GEOID2010) == sorted(rules),
          "and those sixteen are every rule pl94 has, so none is untested")
    wide = parse_geo_line_2010("".join(full))
    check((wide.logrecno, wide.arealand, wide.areawatr)
          == ("0000042", "12345678901234", "98765432109876"),
          "a 14 digit AREALAND and AREAWATR read whole, so neither area "
          "loses its leading digit")
    full[161:167] = list("    12")
    check(composed("700") == "9904512",
          "a VTD the file left-pads with blanks is stripped before it is "
          "joined, as the construction note says it must be")

    wy_logrec, wy_counts = parse_seg1_line(wy_seg1, ",")
    check(wy_logrec == wy.logrecno and wy_counts.total == 91738,
          "the real 2010 segment 1 record is comma delimited, joins on "
          "LOGRECNO and carries P0010001 91738")
    check(race_parts_sum(wy_counts) == 91738,
          "and its seven P1 race columns add to it exactly, so the 2010 "
          "segment layout is the 2020 one")
    wy_logrec2, wy_homes = parse_seg2_line(wy_seg2, ",")
    check(wy_logrec2 == wy.logrecno and wy_homes == (40462, 37576, 2886),
          "the real 2010 segment 2 record carries H1 40462 = 37576 occupied "
          "+ 2886 vacant at the same three positions")

    # ---- P0010002 is a subtotal, not an eighth part
    check(race_parts_sum(counts) == counts.total,
          "the seven P1 race columns add exactly to P1_001")
    check(race_parts_sum(counts) + counts.one_race != counts.total,
          "adding P0010002 to those seven counts the one-race population "
          "twice  <-- pinned defect")
    check("one_race" not in RACE_PARTS,
          "so P0010002 is not in RACE_PARTS at all")

    # ---- the county gate. Summary level first, then the prefix.
    check(prefix_only_match("99045", "045"),
          "the prefix-only filter admits GEOCODE 99045")
    check(prefix_only_match("990450001001001", "045"),
          "and it admits a block in county 045")
    check(not prefix_only_match("99047", "045"),
          "and it rejects the neighbouring county 047")
    check(not prefix_only_match("99", "045"),
          "and a GEOCODE shorter than five characters cannot match")
    by_logrec = dict((parse_geo_line(g).logrecno, parse_geo_line(g))
                     for g in geo_rows)
    sldl, place, block, control = (by_logrec["0000008"], by_logrec["0000009"],
                                   by_logrec["0000005"], by_logrec["0000002"])
    check(prefix_only_match(sldl.geocode, "045")
          and not in_county(sldl, "045"),
          "a SUMLEV 620 legislative district numbered 045 has GEOCODE 99045, "
          "passes the prefix test and is NOT in county 045  <-- pinned defect")
    check(sldl.county == "",
          "because its COUNTY field is empty, which is how the file says so")
    check(prefix_only_match(place.geocode, "045")
          and not in_county(place, "045"),
          "a SUMLEV 160 place with GEOCODE 9904500 passes the prefix test and "
          "is NOT in county 045  <-- pinned defect")
    check(in_county(block, "045"), "a block in county 045 is admitted")
    check(in_county(control, "045"),
          "the county control record is admitted")
    check(not in_county(by_logrec["0000011"], "045"),
          "a block of county 047 in the same state file is dropped")
    cross = by_logrec["0000012"]
    check(in_county(cross, "045")
          and not prefix_only_match(cross.geocode, "045"),
          "a SUMLEV 155 record IS in county 045 even though its GEOCODE is "
          "state + place + county and fails the prefix test  <-- pinned "
          "defect")
    check(not in_county(by_logrec["0000001"], "045"),
          "the state record is in no county at all")
    check(not in_county(by_logrec["0000001"], ""),
          "and an empty county FIPS matches nothing, rather than matching "
          "every record whose COUNTY field is also empty  <-- pinned defect")
    check(not in_county(sldl, ""),
          "which is the same record set the prefix filter would have "
          "called a county")
    check(by_logrec["0000005"].state == "99",
          "STATE is read at 12, two fields before COUNTY, not one  "
          "<-- pinned defect")

    # ---- the whole scan over the synthetic state file
    report = scan(geo_rows, seg_rows, seg2_rows, "045")
    check(report.reconciled,
          "three blocks of 10, 12 and 8 reconcile to a county record of 30")
    check(report.extract_total == 30, "the extract sums to 30")
    check(report.control_total == 30,
          "the control total comes from the file's own SUMLEV 050 row")
    check(report.control_geoid == "99045",
          "and that row is named in the report")
    check(len(report.extract_rows) == 3, "three blocks are extracted")
    check([r[0] for r in report.extract_rows]
          == ["990450001001001", "990450001001002", "990450001001003"],
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
    check(len(CSV_FIELDS) == 15 and "one_race" not in CSV_FIELDS,
          "and the extract carries eight addable population columns, not "
          "nine and not ten, beside the three H1 housing columns")

    # ---- H1 housing and the two areas over the whole extract
    check((report.extract_hu, report.control_hu) == (12, 12),
          "H1 housing units over the three blocks, 4 + 5 + 3, equal the "
          "county record's 12")
    check((report.extract_land, report.control_land) == (3000, 3000)
          and (report.extract_water, report.control_water) == (7, 7),
          "and AREALAND and AREAWATR over them equal the county record's "
          "3000 and 7, summed as numbers")
    check(report.bad_housing == [],
          "every block's occupied and vacant add to its housing total")

    # ---- the neighbouring county, read out of the same file
    other = scan(geo_rows, seg_rows, seg2_rows, "047")
    check(other.reconciled and other.extract_total == 99,
          "county 047 reconciles out of the same state file")
    check([r[0] for r in other.extract_rows] == ["990470001001001"],
          "and its extract holds only its own block")

    # ---- refusals
    missing = scan([g for g in geo_rows
                    if g.split("|")[2] != "050"],
                   seg_rows, seg2_rows, "045")
    check(not missing.reconciled and missing.control_total is None,
          "a file with no SUMLEV 050 row for the county is REFUSED, not "
          "reported against nothing  <-- pinned defect")
    check(any("nothing in it to reconcile against" in r
              for r in missing.reasons),
          "and the refusal says the control record is absent")

    short = scan(geo_rows, without_seg("0000007"), seg2_rows, "045")
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
                seg2_rows, "045")
    check(not over.reconciled and over.extract_total == 30
          and over.control_total == 20,
          "an extract of 30 against a control of 20 is REFUSED")
    check(any("a difference of +10" in r for r in over.reasons),
          "and the difference is signed, so an over-count reads differently "
          "from an under-count of the same size  <-- pinned defect")

    dup = scan(geo_rows + [geo_line("050", "0000013", "99045", "045")],
               seg_rows + [seg_line("0000013", 16, 6, 1, 2, 1, 2, 2, 7)],
               seg2_rows, "045")
    check(not dup.reconciled and any("ambiguous" in r for r in dup.reasons),
          "two SUMLEV 050 rows for one county are REFUSED as ambiguous, "
          "rather than one of them being picked")

    orphan = scan(geo_rows, without_seg("0000002"), seg2_rows, "045")
    check(not orphan.reconciled
          and any("carries no population" in r for r in orphan.reasons),
          "a control record with no segment 1 row is REFUSED")

    lost = scan(geo_rows, without_seg("0000006"), seg2_rows, "045")
    check(not lost.reconciled
          and any("have no segment 1 row" in r for r in lost.reasons),
          "a block with no segment 1 row is REFUSED, not dropped from a sum "
          "that then reconciles to nothing")

    corrupt = scan(geo_rows,
                   without_seg("0000005")
                   + [seg_line("0000005", 4, 2, 1, 1, 0, 0, 1, 3, total=10)],
                   seg2_rows, "045")
    check(corrupt.extract_total == 30,
          "a block whose P1_001 still says 10 while its race columns add to "
          "9 leaves the county total looking right")
    check(not corrupt.reconciled
          and any("do not add to P1_001" in r for r in corrupt.reasons),
          "and it is REFUSED anyway, because the row disagrees with itself  "
          "<-- pinned defect")
    bloated = scan(geo_rows,
                   without_seg("0000005")
                   + [seg_line("0000005", 5, 2, 1, 1, 0, 0, 1, 3, total=9)],
                   seg2_rows, "045")
    check(not bloated.reconciled
          and bloated.bad_race == ["990450001001003"],
          "and so is a block whose race columns add to 10, MORE than its "
          "P1_001 of 9")

    place = scan(geo_rows, seg_rows, seg2_rows, "045", "160")
    check(not place.reconciled
          and any("no record in county 045 carries summary level 160" in r
                  for r in place.reasons),
          "asking for SUMLEV 160 out of a county is REFUSED: the place in "
          "this file is not in the county  <-- pinned defect")
    check(place.extract_rows == [], "and nothing is extracted for it")
    check(any("carries 050, 140, 150, 155, 750 for it" in r
              for r in place.reasons),
          "and the refusal names the levels the file DOES carry for that "
          "county, read off the file rather than off a list in this tool")

    itself = scan(geo_rows, seg_rows, seg2_rows, "045", "050")
    check(not itself.reconciled
          and any("proves nothing" in r for r in itself.reasons),
          "extracting the control record and checking it against itself is "
          "REFUSED  <-- pinned defect")

    empty = scan(geo_rows, seg_rows, seg2_rows, "045", "070")
    check(not empty.reconciled and empty.extract_total == 0,
          "a summary level the file does not carry at all extracts nothing "
          "and is REFUSED, rather than reconciling zero against zero")

    cross_only = scan(geo_rows, seg_rows, seg2_rows, "045", "155")
    check(cross_only.reconciled and cross_only.extract_total == 30,
          "the SUMLEV 155 record the prefix filter would have missed "
          "reconciles as its own extract")

    bg = scan(geo_rows, seg_rows, seg2_rows, "045", "150")
    check(bg.reconciled and bg.extract_total == 30,
          "the block group level reconciles too, so --sumlev is real")

    check(scan(geo_rows + ["", "   "], seg_rows + [""], seg2_rows + [" "],
               "045").reconciled,
          "blank lines at the end of any of the three files are skipped")

    # ---- two files joined together. LOGRECNO is the join key, so a repeat
    # silently replaces a record rather than adding one.
    raises(lambda: scan(geo_rows + [geo_line("750", "0000005",
                                             "990450009009009", "045")],
                        seg_rows, seg2_rows, "045"),
           "a LOGRECNO repeated in the geoheader raises, rather than the "
           "second record replacing the first and a block leaving the "
           "extract without changing the row count  <-- pinned defect")
    raises(lambda: scan(geo_rows,
                        seg_rows + [seg_line("0000005", 99, 0, 0, 0, 0, 0, 0,
                                             0)],
                        seg2_rows, "045"),
           "a LOGRECNO repeated in segment 1 raises, rather than the second "
           "population replacing the first  <-- pinned defect")
    check(scan(geo_rows,
               seg_rows + [seg_line("0009999", 99, 0, 0, 0, 0, 0, 0, 0)],
               seg2_rows, "045").reconciled,
          "while a segment 1 row for a LOGRECNO this county does not carry "
          "is ignored, because the file holds the whole state")
    raises(lambda: scan(geo_rows, seg_rows,
                        seg2_rows + [seg2_line("0000005", 9, 0)], "045"),
           "a LOGRECNO repeated in segment 2 raises too, rather than the "
           "second housing count replacing the first  <-- pinned defect")

    # ---- H1 refusals. Population can reconcile while housing does not.
    homeless = scan(geo_rows, seg_rows, without_seg2("0000006"), "045")
    check(not homeless.reconciled
          and any("have no segment 2 row" in r for r in homeless.reasons),
          "a block with no segment 2 row is REFUSED, not dropped")
    check(len(homeless.extract_rows) == 2,
          "and it is left out of the rows rather than written with blank "
          "housing cells")
    no_hu = scan(geo_rows, seg_rows, without_seg2("0000002"), "045")
    check(not no_hu.reconciled and no_hu.control_hu is None
          and any("carries no housing count" in r for r in no_hu.reasons),
          "a county record with no segment 2 row is REFUSED, and its housing "
          "total is none, not zero")
    extra_home = scan(geo_rows, seg_rows,
                      without_seg2("0000006") + [seg2_line("0000006", 5, 1)],
                      "045")
    check(extra_home.extract_total == extra_home.control_total == 30,
          "a block whose H1 says 6 where the county allows 5 leaves the "
          "population reconciled at 30")
    check(not extra_home.reconciled
          and any("H1 housing units over summary level 750 sums to 13" in r
                  and "a difference of +1" in r
                  for r in extra_home.reasons),
          "and is REFUSED on housing alone, 13 against 12  <-- pinned "
          "defect")
    torn = scan(geo_rows, seg_rows,
                without_seg2("0000005") + [seg2_line("0000005", 2, 1,
                                                     total=4)],
                "045")
    check(not torn.reconciled and torn.bad_housing == ["990450001001003"]
          and any("occupied and vacant do not add" in r
                  for r in torn.reasons),
          "a block whose occupied and vacant add to 3 under a total of 4 is "
          "REFUSED even though the county total still matches  <-- pinned "
          "defect")
    crowded = scan(geo_rows, seg_rows,
                   without_seg2("0000005") + [seg2_line("0000005", 3, 1,
                                                        total=3)],
                   "045")
    check(not crowded.reconciled
          and crowded.bad_housing == ["990450001001003"],
          "and so is one whose occupied and vacant add to 4, MORE than its "
          "total of 3")

    # ---- area refusals. A block with no people and no homes moves
    # neither P1 nor H1, so a filter that drops empty blocks reconciles on
    # both. Only the area checks see it go.
    empty_block = geo_line("750", "0000014", "990450001001004", "045", "500")
    roomy = scan(geo_rows + [empty_block],
                 seg_rows + [seg_line("0000014", 0, 0, 0, 0, 0, 0, 0, 0)],
                 seg2_rows + [seg2_line("0000014", 0, 0)], "045")
    check(roomy.extract_total == 30 and roomy.extract_hu == 12,
          "an extra block of 0 people and 0 housing units leaves P1 and H1 "
          "both reconciled")
    check(not roomy.reconciled
          and any("AREALAND over summary level 750 sums to 3500" in r
                  and "a difference of +500" in r for r in roomy.reasons),
          "and is REFUSED on AREALAND, 3500 square metres against 3000  "
          "<-- pinned defect")
    wet = scan([g.replace("|1000|7|", "|1000|9|") for g in geo_rows],
               seg_rows, seg2_rows, "045")
    check(not wet.reconciled
          and any("AREAWATR over summary level 750 sums to 9" in r
                  for r in wet.reasons),
          "AREAWATR is reconciled the same way, 9 against 7")
    dry = scan([geo_line("050", "0000002", "99045", "045", "3000", "0")
                if g.split("|")[7] == "0000002" else g for g in geo_rows],
               seg_rows, seg2_rows, "045")
    check(not dry.reconciled and dry.control_water == 0
          and any("AREAWATR over summary level 750 sums to 7, the county "
                  "control record 99045 says 0" in r for r in dry.reasons),
          "a county whose AREAWATR is 0 is still a control: blocks with 7 "
          "square metres of water against it are REFUSED, not skipped as if "
          "the county had no figure")
    raises(lambda: scan([g.replace("|1000|7|", "||7|") for g in geo_rows],
                        seg_rows, seg2_rows, "045"),
           "a block with an empty AREALAND raises rather than adding as zero  "
           "<-- pinned defect")
    raises(lambda: scan([g.replace("|3000|7|", "|3000|x|") for g in geo_rows],
                        seg_rows, seg2_rows, "045"),
           "and so does a county record whose AREAWATR is not a number")
    raises(lambda: _whole("-500", "AREALAND", "990450001001001"),
           "and a negative AREALAND, which int() would read and subtract from "
           "the sum  <-- pinned defect")
    raises(lambda: scan(geo_rows, seg_rows, seg2_rows, "045", "750", "2000"),
           "a year with no layout is refused before anything is read")

    # ---- the same synthetic state in the 2010 fixed-width layout. Written
    # at the 1-based positions Figure 2-4 prints, never through GEO2010.
    def geo_line_2010(sumlev, logrec, county="", tract="", blkgrp="",
                      block="", sldl="", place="", land="1000", water="0",
                      name=""):
        f = [" "] * 500

        def put(pos, text):
            f[pos - 1:pos - 1 + len(text)] = list(text)
        put(1, "PLST")
        put(7, "ZZ")
        put(9, sumlev)
        put(19, logrec)
        put(28, "99")
        put(30, county)
        put(46, place)
        put(55, tract)
        put(61, blkgrp)
        put(62, block)
        put(159, sldl)
        put(199, land.rjust(14))
        put(213, water.rjust(14))
        put(227, name)
        return "".join(f)

    # The county's NAME carries one latin-1 byte, n with a tilde, as names in
    # the real 2010 New Mexico and Puerto Rico files do. Read as UTF-8, that
    # byte is an error and the whole file exits 2.
    geo_rows_2010 = [
        geo_line_2010("040", "0000001"),
        geo_line_2010("050", "0000002", "045", land="3000", water="7",
                      name=u"Ca\xf1ada County"),
        geo_line_2010("140", "0000003", "045", "000100", land="3000",
                      water="7"),
        geo_line_2010("150", "0000004", "045", "000100", "1", land="3000",
                      water="7"),
        geo_line_2010("750", "0000005", "045", "000100", block="1003"),
        geo_line_2010("750", "0000006", "045", "000100", block="1001"),
        geo_line_2010("750", "0000007", "045", "000100", block="1002",
                      water="7"),
        geo_line_2010("620", "0000008", sldl="045"),
        geo_line_2010("160", "0000009", place="04500"),
        geo_line_2010("155", "0000012", "045", place="54000", land="3000",
                      water="7"),
        geo_line_2010("050", "0000010", "047"),
        geo_line_2010("750", "0000011", "047", "000100", block="1001"),
    ]
    seg_rows_2010 = [s.replace("|", ",") for s in seg_rows]
    seg2_rows_2010 = [s.replace("|", ",") for s in seg2_rows]

    r2010 = scan(geo_rows_2010, seg_rows_2010, seg2_rows_2010, "045",
                 year="2010")
    check(r2010.reconciled and r2010.year == "2010",
          "the same state written in the 2010 layout reconciles with "
          "year 2010")
    check([r[0] for r in r2010.extract_rows]
          == [r[0] for r in report.extract_rows],
          "its blocks compose the same GEOIDs the 2020 GEOCODE carries, "
          "STATE + COUNTY + TRACT + BLOCK")
    check((r2010.extract_total, r2010.extract_hu, r2010.extract_land,
           r2010.extract_water) == (30, 12, 3000, 7),
          "and reconcile population, housing, land and water to the same "
          "county record")
    check(r2010.prefix_only_total == 127,
          "the composed GEOIDs fall into the same prefix trap, 127 for a "
          "county of 30, because the district and the place compose 99045 "
          "and 9904500")
    check(r2010.extract_rows[0][1].arealand == "1000",
          "a space-padded 2010 AREALAND is written to the extract as 1000, "
          "not as fourteen characters")
    keyless = scan(geo_rows_2010, seg_rows_2010, seg2_rows_2010, "045", "155",
                   "2010")
    check(not keyless.reconciled
          and any("no 2010 rule" in r for r in keyless.reasons),
          "a 2010 SUMLEV 155 extract is REFUSED, because no GEOID rule "
          "exists for it, rather than written under an empty key  <-- "
          "pinned defect")
    raises(lambda: scan(geo_rows_2010, seg_rows_2010, seg2_rows_2010, "045"),
           "a 2010 file read as 2020 raises on its first record")
    raises(lambda: scan(geo_rows, seg_rows, seg2_rows, "045", year="2010"),
           "and a 2020 file read as 2010 raises the same way  <-- pinned "
           "defect")
    raises(lambda: scan(geo_rows_2010, seg_rows, seg2_rows_2010, "045",
                        year="2010"),
           "a pipe-delimited segment 1 read with the 2010 comma raises as "
           "truncated")

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

    raises(lambda: scan(geo_rows, seg_rows, seg2_rows, "83"),
           "a two digit county FIPS is refused before anything is read")
    raises(lambda: scan(geo_rows, seg_rows, seg2_rows, "abc"),
           "a county FIPS that is not digits is refused")
    raises(lambda: scan(geo_rows, seg_rows, seg2_rows, "045", "75"),
           "a two digit summary level is refused")
    raises(lambda: scan(geo_rows, seg_rows, seg2_rows, "045", "sev"),
           "a summary level that is not digits is refused")
    raises(lambda: scan(geo_rows, seg_rows, seg2_rows,
                        u"\u0660\u0668\u0663"),
           "a county in Arabic-Indic digits is refused, although "
           "str.isdigit() accepts it  <-- pinned defect")

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
    check("  H1 housing units  extract / county : 12 / 12" in lines
          and "  AREALAND sq m     extract / county : 3000 / 3000" in lines
          and "  AREAWATR sq m     extract / county : 7 / 7" in lines,
          "housing, land and water are printed beside the county record's")
    check("  H1 housing units  extract / county : 12 / none"
          in describe(missing, "x.zip"),
          "and as none, not zero, when there is no county record")
    lines_2010 = describe(r2010, "state2010.pl.zip")
    check(lines_2010[1] == "  county 045, summary level 750, 2010 layout",
          "a 2010 run says which layout it read")
    check(any(l.startswith("  620")
              and "NO, the composed GEOID prefix let it in" in l
              for l in lines_2010),
          "and names the composed GEOID, not a GEOCODE field 2010 does not "
          "have")
    check(any(l.startswith("  155")
              and l.endswith("yes, and pl94 has no 2010 GEOID rule for it")
              for l in lines_2010),
          "a 2010 level with no GEOID rule is marked as such, not as one the "
          "prefix misses, because it has no key to test  <-- pinned defect")

    # ---- real files on disk. Everything below writes into one temporary
    # directory and deletes it again. No network, no credentials.
    tmp = tempfile.mkdtemp(prefix="pl94-selftest-")
    # Also at exit, so a self-test that crashes partway leaves no directory.
    atexit.register(shutil.rmtree, tmp, True)

    def make_zip(name, geo=None, seg=None, extra=None, seg2=None,
                 year="2020"):
        path = os.path.join(tmp, name)
        with zipfile.ZipFile(path, "w") as zf:
            for member, rows in (("zzgeo%s.pl", geo), ("zz00001%s.pl", seg),
                                 ("zz00002%s.pl", seg2)):
                if rows is not None:
                    zf.writestr(member % year,
                                "\r\n".join(rows).encode("latin-1"))
            for member, body in (extra or []):
                zf.writestr(member, body)
        return path

    def run_cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    # A real 2020 PL zip holds four members: the geoheader and segments 1, 2
    # and 3. Segment 2 is P3, P4 and H1, and pl94 reads its H1. Segment 3 is
    # the group quarters table, ends in "2020.pl" too, and is not read. Its
    # one record here is too short to parse, so reading it would fail.
    other_segments = [
        ("zz000032020.pl", b"PLST|ZZ|000|03|0000005|9|9|9\r\n"),
    ]
    good_zip = make_zip("state2020.pl.zip", geo_rows, seg_rows,
                        other_segments, seg2_rows)
    short_zip = make_zip("short.pl.zip", geo_rows, without_seg("0000007"),
                         seg2=seg2_rows)

    check(sorted(zipfile.ZipFile(good_zip).namelist())
          == ["zz000012020.pl", "zz000022020.pl", "zz000032020.pl",
              "zzgeo2020.pl"],
          "the zip holds the four members a real PL download holds: the "
          "geoheader and segments 1, 2 and 3")

    code, out, err = run_cli(["--pl", good_zip, "--county", "045"])
    check(code == 0, "the real zip reconciles through the command line, so "
                     "segment 2 is read as segment 2 and segment 3 is passed "
                     "over, neither read as more segment 1  <-- pinned "
                     "defect")
    check("RECONCILED" in out, "and says so")
    check("the GEOCODE prefix admits: 127" in out,
          "and prints the 127 it refused to call a population")
    check(err == "", "a clean run writes nothing to stderr")

    code, out, err = run_cli(["--pl", short_zip, "--county", "045"])
    check(code == 1, "a zip whose blocks do not reconcile exits 1")
    check("REFUSED" in out and "difference of -8" in out,
          "and prints the difference it found")

    # ---- the extract file itself
    dest = os.path.join(tmp, "blocks.csv")
    code, out, err = run_cli(["--pl", good_zip, "--county", "045",
                              "--out", dest])
    check(code == 0 and not os.path.exists(dest),
          "--out without --apply writes nothing  <-- pinned defect")
    check("was not written" in out, "and says the file was not written")
    code, out, err = run_cli(["--pl", short_zip, "--county", "045",
                              "--out", os.path.join(tmp, "never.csv")])
    check(code == 1 and "--apply would refuse it too" in out
          and "Re-run with --apply" not in out,
          "a check-only run of an extract that does not reconcile does not "
          "tell the operator to re-run with --apply  <-- pinned defect")

    code, out, err = run_cli(["--pl", good_zip, "--county", "045",
                              "--out", dest, "--apply"])
    check(code == 0 and os.path.exists(dest), "--apply writes the extract")
    with open(dest, "r") as fh:
        body = fh.read()
    csv_rows = list(csv.reader(body.splitlines()))
    check(csv_rows[0] == ["geoid", "arealand", "areawatr", "total",
                          "white", "black", "aian", "asian", "nhpi",
                          "other", "two_or_more", "hispanic_any_race",
                          "housing_units", "occupied", "vacant"],
          "the header is these fifteen columns in this order, named here "
          "rather than read back out of the constant that wrote them")
    check(len(csv_rows) == 4, "three block rows under it")
    check(all(len(r) == 15 for r in csv_rows),
          "and every row carries exactly fifteen values, so a sixteenth "
          "column cannot ride in under a fifteen column header  "
          "<-- pinned defect")
    check(csv_rows[1][12:] == ["5", "5", "0"],
          "the H1 columns carry the block's own housing, occupied and vacant")
    check(csv_rows[1][3:12] == ["12", "7", "3", "0", "1", "1", "0", "0", "2"],
          "and total, the seven race columns and hispanic_any_race carry the "
          "block's own 12, 7, 3, 0, 1, 1, 0, 0 and 2, each in its own column")
    check(sum(int(r[12]) for r in csv_rows[1:]) == 12,
          "and the written housing column adds up to the county's 12")
    check(csv_rows[1][0] == "990450001001001" and csv_rows[1][3] == "12",
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
    code, out, err = run_cli(["--pl", short_zip, "--county", "045",
                              "--out", refused, "--apply"])
    check(code == 1 and not os.path.exists(refused),
          "--apply does NOT write an extract that failed to reconcile  "
          "<-- pinned defect")
    check("Refused" in out and "--allow-unreconciled" in out,
          "and the refusal names the flag that would override it")

    code, out, err = run_cli(["--pl", short_zip, "--county", "045",
                              "--out", refused, "--apply",
                              "--allow-unreconciled"])
    check(os.path.exists(refused), "--allow-unreconciled writes it anyway")
    check(code == 1,
          "and still exits 1, because the extract is still wrong  "
          "<-- pinned defect")

    bg_csv = os.path.join(tmp, "bg.csv")
    code, out, err = run_cli(["--pl", good_zip, "--county", "045",
                              "--sumlev", "150", "--out", bg_csv, "--apply"])
    check(code == 0, "--sumlev 150 extracts the block group and reconciles")
    with open(bg_csv, "r") as fh:
        check(len(fh.read().splitlines()) == 2,
              "and writes one block group row under the header")

    code, out, err = run_cli(["--pl", good_zip, "--county", "047"])
    check(code == 0 and "99047" in out,
          "the neighbouring county reconciles out of the same zip")

    # ---- unreadable input
    code, out, err = run_cli(["--pl", os.path.join(tmp, "nope.zip"),
                              "--county", "045"])
    check(code == 2 and "error:" in err, "a zip that is not there exits 2")

    not_a_zip = os.path.join(tmp, "plain.txt")
    with open(not_a_zip, "w") as fh:
        fh.write("this is not a zip file\n")
    code, out, err = run_cli(["--pl", not_a_zip, "--county", "045"])
    check(code == 2, "a file that is not a zip exits 2")

    no_geo = make_zip("nogeo.zip", None, seg_rows, seg2=seg2_rows)
    code, out, err = run_cli(["--pl", no_geo, "--county", "045"])
    check(code == 2 and "geoheader" in err,
          "a zip with no geoheader file exits 2 and says which file is "
          "missing")

    no_seg = make_zip("noseg.zip", geo_rows, None, seg2=seg2_rows)
    code, out, err = run_cli(["--pl", no_seg, "--county", "045"])
    check(code == 2 and "segment 1" in err,
          "a zip with no segment 1 file exits 2 and says so")

    no_seg2 = make_zip("noseg2.zip", geo_rows, seg_rows)
    code, out, err = run_cli(["--pl", no_seg2, "--county", "045"])
    check(code == 2 and "no 2020 segment 2 file" in err,
          "a zip with no segment 2 file exits 2, rather than reconciling "
          "population and skipping housing  <-- pinned defect")

    two_geo = make_zip("twogeo.zip", geo_rows, seg_rows,
                       [("__MACOSX/zzgeo2020.pl", b"junk")], seg2_rows)
    code, out, err = run_cli(["--pl", two_geo, "--county", "045"])
    check(code == 2 and "look like the 2020 geoheader" in err,
          "two files that both look like the geoheader are REFUSED, not "
          "resolved by taking the first  <-- pinned defect")
    two_seg2 = make_zip("twoseg2.zip", geo_rows, seg_rows,
                        [("old/zz000022020.pl", b"junk")], seg2_rows)
    code, out, err = run_cli(["--pl", two_seg2, "--county", "045"])
    check(code == 2 and "look like the 2020 segment 2" in err,
          "and so are two files that both look like segment 2")
    shouty = os.path.join(tmp, "shouty.zip")
    with zipfile.ZipFile(shouty, "w") as zf:
        for member, rows in (("ZZGEO2020.PL", geo_rows),
                             ("ZZ000012020.PL", seg_rows),
                             ("ZZ000022020.PL", seg2_rows)):
            zf.writestr(member, "\r\n".join(rows).encode("latin-1"))
    code, out, err = run_cli(["--pl", shouty, "--county", "045"])
    check(code == 0,
          "member names in upper case, such as ZZGEO2020.PL, are matched")

    truncated = make_zip("trunc.zip", geo_rows,
                         seg_rows[:-1] + ["|".join(["0"] * 100)],
                         seg2=seg2_rows)
    code, out, err = run_cli(["--pl", truncated, "--county", "045"])
    check(code == 2 and "truncated" in err,
          "a truncated segment 1 record exits 2 rather than reading a short "
          "record as zeros")

    truncated2 = make_zip("trunc2.zip", geo_rows, seg_rows,
                          seg2=seg2_rows[:-1] + ["|".join(["0"] * 151)])
    code, out, err = run_cli(["--pl", truncated2, "--county", "045"])
    check(code == 2 and "segment 2 record has 151 fields" in err,
          "and so does a truncated segment 2 record")

    # ---- the 2010 zip, end to end
    zip_2010 = make_zip("state2010.pl.zip", geo_rows_2010, seg_rows_2010,
                        seg2=seg2_rows_2010, year="2010")
    csv_2010 = os.path.join(tmp, "blocks2010.csv")
    code, out, err = run_cli(["--pl", zip_2010, "--county", "045",
                              "--year", "2010", "--out", csv_2010,
                              "--apply"])
    check(code == 0 and "RECONCILED" in out and "2010 layout" in out,
          "a 2010 zip reconciles through the command line with --year 2010")
    with zipfile.ZipFile(zip_2010) as zf:
        check(b"Ca\xf1ada County" in zf.read("zzgeo2010.pl"),
              "and its geoheader carries the latin-1 byte 0xF1 in a county "
              "name, so the file is read as latin-1, not UTF-8")
    with open(csv_2010, "r") as fh:
        rows_2010 = list(csv.reader(fh.read().splitlines()))
    check(rows_2010[1:] == csv_rows[1:],
          "and writes the same rows, byte for byte, as the same state in "
          "2020 form: same GEOIDs, unpadded areas, same counts")
    code, out, err = run_cli(["--pl", zip_2010, "--county", "045"])
    check(code == 2 and "zzgeo2010.pl" in err,
          "the same zip without --year exits 2 and names the 2010 members it "
          "does hold, so the fix is obvious  <-- pinned defect")
    code, out, err = run_cli(["--pl", good_zip, "--county", "045",
                              "--year", "2010"])
    check(code == 2 and "zzgeo2020.pl" in err,
          "and a 2020 zip read with --year 2010 exits 2 the same way")
    code, out, err = run_cli(["--pl", make_zip("empty.zip"), "--county",
                              "045"])
    check(code == 2 and "which holds nothing" in err,
          "an empty zip says it holds nothing")

    # ---- damaged zips. zipfile raises three things that are not OSError
    # for a member it cannot read, and each one used to escape as a
    # traceback with exit 1, which means "the numbers disagreed".
    deflated = os.path.join(tmp, "deflated.zip")
    with zipfile.ZipFile(deflated, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, rows in (("zzgeo2020.pl", geo_rows),
                             ("zz000012020.pl", seg_rows),
                             ("zz000022020.pl", seg2_rows)):
            zf.writestr(member, "\r\n".join(rows).encode("latin-1"))
    check(run_cli(["--pl", deflated, "--county", "045"])[0] == 0,
          "a deflated copy of the good zip reconciles")
    with open(deflated, "rb") as fh:
        pristine = fh.read()

    def damaged(name, patch):
        path = os.path.join(tmp, name)
        data = bytearray(pristine)
        patch(data)
        with open(path, "wb") as fh:
            fh.write(bytes(data))
        return run_cli(["--pl", path, "--county", "045"])

    def headers(data, local_off, central_off, value):
        for sig, off in ((b"PK\x03\x04", local_off),
                         (b"PK\x01\x02", central_off)):
            at = data.find(sig)
            while at != -1:
                data[at + off] = value
                at = data.find(sig, at + 4)

    def bad_block(data):
        # 0xFF opens a deflate block of type 3, which the format reserves.
        name_len = data[26] + 256 * data[27]
        extra_len = data[28] + 256 * data[29]
        data[30 + name_len + extra_len] = 0xFF

    code, out, err = damaged("badblock.zip", bad_block)
    check(code == 2 and "error" in err and "Traceback" not in err,
          "a geoheader member whose deflate stream is damaged exits 2 with "
          "one line, not a traceback that exits 1  <-- pinned defect")
    code, out, err = damaged("locked.zip",
                             lambda d: headers(d, 6, 8, 0x01))
    check(code == 2 and "encrypted" in err,
          "an encrypted member exits 2 and says it is encrypted  <-- pinned "
          "defect")
    code, out, err = damaged("deflate64.zip",
                             lambda d: headers(d, 8, 10, 9))
    check(code == 2 and "compression method is not supported" in err,
          "a Deflate64 member, which zipfile cannot read, exits 2  <-- "
          "pinned defect")

    lzma_zip = os.path.join(tmp, "lzma.zip")
    with zipfile.ZipFile(lzma_zip, "w", zipfile.ZIP_LZMA) as zf:
        for member, rows in (("zzgeo2020.pl", geo_rows),
                             ("zz000012020.pl", seg_rows),
                             ("zz000022020.pl", seg2_rows)):
            zf.writestr(member, "\r\n".join(rows).encode("latin-1"))
    with open(lzma_zip, "rb") as fh:
        data = bytearray(fh.read())
    # After the 30 byte header, the name and the 9 byte LZMA properties, the
    # range coder's first byte must be 0. 0xFF is corrupt input to lzma.
    data[30 + data[26] + 256 * data[27] + data[28] + 256 * data[29] + 9] = 0xFF
    with open(lzma_zip, "wb") as fh:
        fh.write(bytes(data))
    code, out, err = run_cli(["--pl", lzma_zip, "--county", "045"])
    check(code == 2 and "Corrupt input data" in err and "Traceback" not in err,
          "a damaged LZMA member exits 2 with one line, not an LZMAError "
          "traceback that exits 1  <-- pinned defect")

    victim = os.path.join(tmp, "victim.zip")
    shutil.copyfile(good_zip, victim)
    with open(victim, "rb") as fh:
        victim_bytes = fh.read()
    code, out, err = run_cli(["--pl", victim, "--county", "045",
                              "--out", victim, "--apply"])
    with open(victim, "rb") as fh:
        check(code == 64 and fh.read() == victim_bytes
              and "destroy the input" in err,
              "--out naming the --pl zip itself is a usage error, and the zip "
              "is left byte for byte as it was  <-- pinned defect")
    alias = os.path.join(tmp, "alias.zip")
    os.link(victim, alias)
    code, out, err = run_cli(["--pl", victim, "--county", "045",
                              "--out", alias, "--apply"])
    with open(victim, "rb") as fh:
        check(code == 64 and fh.read() == victim_bytes,
              "and so is an --out that names the same file by another path, "
              "here a hard link, which a comparison of path strings misses")

    code, out, err = run_cli(["--pl", good_zip, "--county", "045",
                              "--out", tmp, "--apply"])
    check(code == 2 and "could not write" in err,
          "an extract that cannot be written exits 2, not 1, because 1 means "
          "the numbers disagreed")

    # Two exceptions no offline file here can raise: EOFError from zipfile
    # on a member cut short, and csv.Error from the writer. Each is put in
    # place of the function that would raise it, so both except clauses run.
    def stubbed(name, exc, argv):
        real = globals()[name]

        def boom(*args):
            raise exc("stubbed %s" % name)
        globals()[name] = boom
        try:
            return run_cli(argv)
        finally:
            globals()[name] = real

    code, out, err = stubbed("read_pl", EOFError,
                             ["--pl", good_zip, "--county", "045"])
    check(code == 2 and "error: stubbed read_pl" in err,
          "an EOFError while the zip is read exits 2 with one line")
    code, out, err = stubbed("write_csv", csv.Error,
                             ["--pl", good_zip, "--county", "045",
                              "--out", dest, "--apply"])
    check(code == 2 and "could not write" in err,
          "and a csv.Error while the extract is written exits 2 as well")
    # 0x81 is a byte cp1252 cannot encode. Written in the locale's encoding,
    # it raised on Windows after the header was out, and left a partial CSV.
    odd_csv = os.path.join(tmp, "odd.csv")
    first = report.extract_rows[0]
    write_csv(odd_csv, [(u"\x81" + first[0],) + first[1:]])
    with open(odd_csv, "rb") as fh:
        check(fh.read().splitlines()[1].startswith(b"\x81990450001001001,"),
              "the extract is written as latin-1 on every platform, so a "
              "byte the file gave goes back out as that byte  <-- pinned "
              "defect")

    # ---- usage errors
    check(run_cli([])[0] == 64, "no arguments at all is a usage error")
    check(run_cli(["--county", "045"])[0] == 64, "--pl is required")
    check(run_cli(["--pl", good_zip])[0] == 64, "--county is required")
    check(run_cli(["--pl", good_zip, "--county", "83"])[0] == 64,
          "a two digit county is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "0045"])[0] == 64,
          "a four digit county is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "08x"])[0] == 64,
          "a county that is not digits is a usage error")
    check(run_cli(["--pl", good_zip, "--county",
                   u"\u0660\u0668\u0663"])[0] == 64,
          "and so is one in Arabic-Indic digits, rather than a run that "
          "matches nothing and then cannot print its own report  <-- pinned "
          "defect")
    check(run_cli(["--pl", good_zip, "--county", "045",
                   "--sumlev", "75"])[0] == 64,
          "a two digit summary level is a usage error")
    check(run_cli(["--pl", good_zip, "--county", "045", "--apply"])[0] == 64,
          "--apply without --out is a usage error, not a silent no-op")
    check(run_cli(["--pl", good_zip, "--county", "045",
                   "--year", "2000"])[0] == 64,
          "a year with no layout is a usage error")

    # ---- abbreviations. argparse reads a unique prefix as the whole flag
    # unless told not to, so --ap was --apply.
    def cli_exit(argv):
        """main()'s exit code, including the one argparse exits with."""
        try:
            return run_cli(argv)[0]
        except SystemExit as exc:
            return exc.code

    ap_dest = os.path.join(tmp, "abbrev.csv")
    check(cli_exit(["--pl", good_zip, "--county", "045", "--out", ap_dest,
                    "--ap"]) == 2 and not os.path.exists(ap_dest),
          "--ap is refused by argparse with exit 2 and writes nothing, "
          "rather than being read as --apply  <-- pinned defect")
    check(cli_exit(["--app"]) == 2 and cli_exit(["--appl"]) == 2,
          "and so are --app and --appl")
    check(cli_exit(["--allow"]) == 2,
          "and --allow, which was --allow-unreconciled")
    check(cli_exit(["--apply"]) == 64,
          "while --apply spelled out still parses, and reaches the usage "
          "check for a missing --pl")

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

    # ---- a report nobody can read. A report this size sits in the stdout
    # buffer until the interpreter exits, and a flush that fails THEN exits
    # 120, or 0 on some hosts, whatever the verdict was.
    class FullDisk(io.StringIO):
        def flush(self):
            raise OSError(28, "No space left on device")

    class LateFull(io.StringIO):
        flushes = 0

        def flush(self):
            LateFull.flushes += 1
            if LateFull.flushes > 1:
                raise OSError(28, "No space left on device")

    class LostPipe(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    class LostPipeFd(LostPipe):
        def fileno(self):
            return spare

    def lost_run(stream, argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(stream), \
                contextlib.redirect_stderr(err):
            code = main(argv)
        return code, err.getvalue()

    code, err = lost_run(FullDisk(), ["--pl", good_zip, "--county", "045"])
    check(code == 2 and "could not write the report to stdout" in err,
          "a reconciled run whose report cannot be flushed exits 2, not 0  "
          "<-- pinned defect")
    lost_dest = os.path.join(tmp, "lost.csv")
    code, err = lost_run(FullDisk(), ["--pl", good_zip, "--county", "045",
                                      "--out", lost_dest, "--apply"])
    check(code == 2 and not os.path.exists(lost_dest),
          "and its CSV is not written, because the report is flushed first")
    late_dest = os.path.join(tmp, "late.csv")
    code, err = lost_run(LateFull(), ["--pl", good_zip, "--county", "045",
                                      "--out", late_dest, "--apply"])
    check(code == 2 and os.path.exists(late_dest),
          "a run that loses only its last line, after the CSV is written, "
          "still exits 2, and the CSV stays where it was written")
    code, err = lost_run(LostPipe(), ["--pl", short_zip, "--county", "045"])
    check(code == 2 and "Broken pipe" in err,
          "a write to a closed pipe exits 2 as well, even on a run that "
          "would have exited 1")
    spare_path = os.path.join(tmp, "spare.txt")
    spare = os.open(spare_path, os.O_WRONLY | os.O_CREAT)
    code, err = lost_run(LostPipeFd(), ["--pl", good_zip, "--county", "045"])
    os.write(spare, b"late")
    os.close(spare)
    check(code == 2 and os.path.getsize(spare_path) == 0,
          "and the dead descriptor is pointed at the null device, so the "
          "flush at interpreter exit has nowhere to fail  <-- pinned defect")

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
    check(_parse(["--county", "045"]).county == "045", "--county is read")
    check(_parse(["--sumlev", "150"]).sumlev == "150", "--sumlev is read")
    check(_parse(["--out", "b.csv"]).out == "b.csv", "--out is read")
    check(args.year == "2020",
          "the default year is 2020, the only layout earlier releases read, "
          "so a run without --year behaves as it always did")
    check(_parse(["--year", "2010"]).year == "2010", "--year is read")

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

    def summary():
        """The footer, and the exit code that goes with it."""
        print("-" * 68)
        total = passed[0] + len(failed)
        if failed:
            print("%d assertions, %d failed" % (total, len(failed)))
            for label in failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    kept_passed, kept_failed = passed[0], list(failed)
    noise = io.StringIO()
    with contextlib.redirect_stdout(noise):
        probe()
        probe_code = summary()
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
    check(probe_code == 1
          and "  FAILED: a false check must be recorded as a failure"
          in noise.getvalue().splitlines(),
          "and the footer then exits 1 and lists every failed label, so the "
          "red path of the summary itself has run")

    # ---- importing the file runs nothing. Run under another __name__, the
    # module defines its functions and stops: no sys.exit, no output. It is
    # compiled here rather than imported, so no bytecode cache is written.
    with open(os.path.abspath(__file__), "r") as fh:
        source = fh.read()
    imported = {"__name__": "pl94_imported"}
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        exec(compile(source, os.path.abspath(__file__), "exec"), imported)
    check(imported["YEARS"] == YEARS and quiet.getvalue() == "",
          "importing pl94 as a module runs nothing and prints nothing")

    return summary()


# ----------------------------------------------------------------------- cli

def _is_geo(basename, year):
    """The geoheader member of a PL zip, e.g. dcgeo2020.pl or wygeo2010.pl."""
    return basename.endswith("geo%s.pl" % year)


def _is_seg(basename, number, year):
    """Segment number of a PL zip, e.g. dc000012020.pl or wy000022010.pl."""
    return basename.endswith("%05d%s.pl" % (number, year))


def _member(zf, predicate, what):
    """The one member matching predicate. Zero or several is a refusal.

    Taking the first match is the tempting version and it is wrong: a zip
    re-made on a Mac carries a __MACOSX copy of every file, and the first
    match is then a resource fork that parses as an empty geoheader.
    """
    hits = [name for name in zf.namelist()
            if predicate(os.path.basename(name).lower())]
    if not hits:
        # Name what IS there. A 2010 zip read as 2020 is the usual cause, and
        # its member names say so at a glance.
        raise MissingMember("no %s file in this zip, which holds %s"
                            % (what, ", ".join(sorted(zf.namelist()))
                               or "nothing"))
    if len(hits) > 1:
        raise MissingMember("%d files in this zip look like the %s: %s"
                            % (len(hits), what, ", ".join(sorted(hits))))
    return hits[0]


def read_pl(zip_path, county_fips, sumlev, year=DEFAULT_YEAR):
    """Reconcile one county out of a legacy-format PL zip for one year."""
    with zipfile.ZipFile(zip_path) as zf:
        geo_name = _member(zf, lambda b: _is_geo(b, year),
                           "%s geoheader" % year)
        seg_name = _member(zf, lambda b: _is_seg(b, 1, year),
                           "%s segment 1" % year)
        seg2_name = _member(zf, lambda b: _is_seg(b, 2, year),
                            "%s segment 2" % year)
        with zf.open(geo_name) as geo_fh, zf.open(seg_name) as seg_fh, \
                zf.open(seg2_name) as seg2_fh:
            return scan(geo_fh, seg_fh, seg2_fh, county_fips, sumlev, year)


def write_csv(path, rows):
    """Write the extract. newline='' or every row gains a blank line."""
    # latin-1 both ways: every value was decoded as latin-1, so each one
    # encodes back to the bytes the file gave, on any platform.
    with open(path, "w", newline="", encoding="latin-1") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for geoid, geo, counts, homes in rows:
            writer.writerow([geoid, geo.arealand, geo.areawatr, counts.total,
                             counts.white, counts.black, counts.aian,
                             counts.asian, counts.nhpi, counts.other,
                             counts.two, counts.hispanic, homes.total,
                             homes.occupied, homes.vacant])


def _same_file(a, b):
    """True when two paths name one existing file."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="pl94.py",
        description="Extract one county out of a Census PL 94-171 bulk file, "
                    "and refuse an extract that does not reconcile to the "
                    "county record in the same file.",
        epilog="Nothing is written without --apply, and an extract that does "
               "not reconcile is not written even then.",
        # Without this, --ap, --app and --appl are all read as --apply, so a
        # half-typed flag writes the file.
        allow_abbrev=False,
    )
    ap.add_argument("--pl",
                    help="the PL 94-171 legacy-format state zip, as "
                         "downloaded from the Census Bureau")
    ap.add_argument("--year", default=DEFAULT_YEAR,
                    help="the census year of --pl: 2020 (pipe delimited, the "
                         "default) or 2010 (fixed width)")
    ap.add_argument("--county",
                    help="three digit county FIPS, e.g. 021. Required: there "
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
    try:
        code = _run(args)
        sys.stdout.flush()
    except OSError as exc:
        # stdout is gone: a reader that exited, or a full disk. Left alone,
        # a report small enough to sit in the buffer fails only at
        # interpreter exit, which exits 120, or 0 on some hosts, whatever
        # the verdict. Neither code says the report was lost.
        print("error: could not write the report to stdout: %s" % exc,
              file=sys.stderr)
        try:
            # Point the descriptor at the null device, so the interpreter's
            # own flush at exit has somewhere to put what is still buffered.
            target = sys.stdout.fileno()
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, target)
            os.close(null)
        except (OSError, ValueError):
            pass    # a stdout with no descriptor, such as a StringIO
        return 2
    return code


def _run(args):
    if args.self_test:
        return self_test()

    if not args.pl:
        print("error: --pl is required. Use --self-test to verify the tool "
              "without a Census download.", file=sys.stderr)
        return 64
    if not args.county:
        print("error: --county is required, as three digits, e.g. 021.",
              file=sys.stderr)
        return 64
    if not _three_digits(args.county):
        print("error: --county must be three digits, got %r."
              % (args.county,), file=sys.stderr)
        return 64
    if not _three_digits(args.sumlev):
        print("error: --sumlev must be three digits, got %r."
              % (args.sumlev,), file=sys.stderr)
        return 64
    if args.year not in YEARS:
        print("error: --year must be one of %s, got %r."
              % (", ".join(YEARS), args.year), file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out. There is nothing else to apply.",
              file=sys.stderr)
        return 64
    if args.out and _same_file(args.out, args.pl):
        # The zip is read in full before the CSV is opened, so this would
        # "work": the extract lands and the only copy of the input is gone.
        print("error: --out is the --pl zip itself. Writing the extract "
              "there would destroy the input.", file=sys.stderr)
        return 64

    try:
        report = read_pl(args.pl, args.county, args.sumlev, args.year)
    except (IOError, OSError, ValueError, zipfile.BadZipFile, zlib.error,
            LZMA_ERROR, EOFError, RuntimeError) as exc:
        # zlib.error and LZMA_ERROR are a damaged member, RuntimeError an
        # encrypted one, and NotImplementedError (a RuntimeError) a
        # compression method zipfile cannot read, such as Deflate64. None is
        # an OSError, and each one left alone is a traceback that exits 1,
        # the code for "the numbers disagreed".
        print("error: %s" % exc, file=sys.stderr)
        return 2

    for line in describe(report, os.path.basename(args.pl)):
        print(line)
    # Before the CSV, so a report nobody can read also stops the write.
    sys.stdout.flush()

    refused = not report.reconciled and not args.allow_unreconciled
    if args.out:
        if not args.apply and refused:
            print("\nCheck only. %s was not written, and --apply would "
                  "refuse it too, because the extract does not reconcile."
                  % args.out)
        elif not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        elif refused:
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
