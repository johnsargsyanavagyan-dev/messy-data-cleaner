#!/usr/bin/env python3
"""Messy Data Cleaner.

Applies rules.yaml to every row of a CSV and writes:
  clean.csv         rows with no open issues (certain fixes applied)
  change_log.csv    every change made: row_id, column, old_value, new_value, rule_id
  needs_review.csv  every uncertain case, one line per issue, never changed by guessing

Principle: automate what is certain, escalate what is uncertain.

Usage:
  python clean.py messy.csv --rules rules.yaml --out output/
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime

try:
    import yaml
except ImportError:
    sys.exit("This script needs PyYAML. Install it with: pip install pyyaml")

REASONS = {
    "REQUIRED_BLANK": "required field is blank",
    "EMAIL_INVALID": "email does not look valid",
    "CITY_UNKNOWN": "city spelling not in the approved list or known variants",
    "DATE_AMBIGUOUS": "date could be day/month or month/day",
    "DATE_INVALID": "date is impossible or in the future",
    "DATE_UNKNOWN_FORMAT": "date format not in the accepted list",
    "NOT_A_NUMBER": "value should be a whole number",
    "OUT_OF_RANGE": "number outside the allowed range",
    "VALUE_NOT_ALLOWED": "value not in the allowed list",
    "DUP_SAME_NAME": "same name as another row but different details",
    "DUP_SAME_EMAIL": "same email as another row but different name",
}


def title_name(s):
    return " ".join("-".join(p.capitalize() for p in w.split("-")) for w in s.split(" "))


class Cleaner:
    def __init__(self, rules):
        self.r = rules
        city = rules.get("city") or {}
        self.city_col = city.get("column")
        self.city_approved = {c.casefold(): c for c in city.get("approved", [])}
        self.city_variants = {k.casefold(): v for k, v in (city.get("variants") or {}).items()}
        email = rules.get("email") or {}
        self.email_col = email.get("column")
        self.email_re = re.compile(email["pattern"]) if email.get("pattern") else None
        dates = rules.get("dates") or {}
        self.date_cols = dates.get("columns", [])
        self.date_out = dates.get("output_format", "%Y-%m-%d")
        self.date_formats = dates.get("accepted_formats", [])
        self.ranges = rules.get("ranges") or {}
        self.allowed = {c: {v.casefold(): v for v in vals}
                        for c, vals in (rules.get("allowed_values") or {}).items()}
        self.required = rules.get("required_fields", [])
        self.name_col = rules.get("name_column")

    # ---- one cell: returns (new_value, [rule_ids], issue_code or None)
    def fix_cell(self, col, value):
        v, applied, issue = value, [], None
        if self.r.get("trim_spaces", True):
            t = re.sub(r"\s+", " ", v.strip())
            if t != v:
                v = t
                applied.append("TRIM")
        if v == "":
            return v, applied, ("REQUIRED_BLANK" if col in self.required else None)

        if col == self.name_col and (v.islower() or v.isupper()):
            v = title_name(v)
            applied.append("NAME_CASE")

        if col == self.email_col:
            if v != v.lower():
                v = v.lower()
                applied.append("EMAIL_LOWER")
            if self.email_re and not self.email_re.match(v):
                issue = "EMAIL_INVALID"

        if col == self.city_col:
            key = v.casefold()
            canon = self.city_approved.get(key) or self.city_variants.get(key)
            if canon is None:
                issue = "CITY_UNKNOWN"
            elif canon != v:
                v = canon
                applied.append("CITY_MAP")

        if col in self.date_cols:
            new, issue = self.parse_date(v)
            if new is not None and new != v:
                v = new
                applied.append("DATE_FORMAT")

        if col in self.ranges:
            try:
                n = int(v)
                lo, hi = self.ranges[col].get("min"), self.ranges[col].get("max")
                if (lo is not None and n < lo) or (hi is not None and n > hi):
                    issue = "OUT_OF_RANGE"
            except ValueError:
                issue = "NOT_A_NUMBER"

        if col in self.allowed:
            canon = self.allowed[col].get(v.casefold())
            if canon is None:
                issue = "VALUE_NOT_ALLOWED"
            elif canon != v:
                v = canon
                applied.append("VALUE_CASE")

        return v, applied, issue

    def parse_date(self, v):
        """Returns (iso_string or None, issue or None)."""
        d = None
        for fmt in self.date_formats:
            try:
                d = datetime.strptime(v, fmt).date()
                break
            except ValueError:
                pass
        if d is None:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
            if not m:
                return None, "DATE_UNKNOWN_FORMAT"
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12 and b <= 12:
                day, month = a, b
            elif b > 12 and a <= 12:
                month, day = a, b
            elif a == b:
                day = month = a
            elif a <= 12 and b <= 12:
                return None, "DATE_AMBIGUOUS"
            else:
                return None, "DATE_INVALID"
            try:
                d = date(y, month, day)
            except ValueError:
                return None, "DATE_INVALID"
        if d > date.today():
            return None, "DATE_INVALID"
        return d.strftime(self.date_out), None


def run(input_path, rules_path, out_dir):
    with open(rules_path, encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    cl = Cleaner(rules)

    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
        originals = [dict(r) for r in reader]
    for row in originals:  # missing cells -> ""
        for c in columns:
            row[c] = row.get(c) or ""

    log, issues = [], defaultdict(list)  # issues[row_id] = [(col, code, related)]
    rows = {}
    for i, orig in enumerate(originals, start=1):
        new = {}
        for c in columns:
            val, applied, issue = cl.fix_cell(c, orig[c])
            new[c] = val
            if applied:
                log.append({"row_id": i, "column": c, "old_value": orig[c],
                            "new_value": val, "rule_id": "+".join(applied)})
            if issue:
                issues[i].append((c, issue, ""))
        rows[i] = new

    # exact duplicates (after certain fixes): keep first, remove later copies
    removed, seen = {}, {}
    for i, new in rows.items():
        key = tuple(new[c] for c in columns)
        if key in seen:
            removed[i] = seen[key]
            log.append({"row_id": i, "column": "*", "old_value": "(whole row)",
                        "new_value": f"removed: exact duplicate of row {seen[key]}",
                        "rule_id": "DUP_EXACT"})
            issues.pop(i, None)
        else:
            seen[key] = i

    # conflicting near-duplicates -> review both rows
    kept = [i for i in rows if i not in removed]
    dup = rules.get("duplicates") or {}
    if dup.get("same_name_different_details") == "review" and cl.name_col:
        groups = defaultdict(list)
        for i in kept:
            if rows[i][cl.name_col]:
                groups[rows[i][cl.name_col].casefold()].append(i)
        for ids in groups.values():
            if len(ids) > 1:
                for i in ids:
                    issues[i].append((cl.name_col, "DUP_SAME_NAME",
                                      ",".join(str(j) for j in ids if j != i)))
    if dup.get("same_email_different_name") == "review" and cl.email_col:
        groups = defaultdict(list)
        for i in kept:
            if rows[i][cl.email_col]:
                groups[rows[i][cl.email_col]].append(i)
        for ids in groups.values():
            names = {rows[i].get(cl.name_col, "").casefold() for i in ids}
            if len(ids) > 1 and len(names) > 1:
                for i in ids:
                    issues[i].append((cl.email_col, "DUP_SAME_EMAIL",
                                      ",".join(str(j) for j in ids if j != i)))

    clean_ids = [i for i in kept if i not in issues]
    review_ids = [i for i in kept if i in issues]

    # ---- write outputs
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "clean.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["row_id"] + columns)
        w.writeheader()
        for i in clean_ids:
            w.writerow({"row_id": i, **rows[i]})
    with open(os.path.join(out_dir, "change_log.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["row_id", "column", "old_value", "new_value", "rule_id"])
        w.writeheader()
        w.writerows(sorted(log, key=lambda e: e["row_id"]))
    review_fields = ["row_id", "column", "value", "reason_code", "reason", "related_rows"] + columns
    with open(os.path.join(out_dir, "needs_review.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=review_fields)
        w.writeheader()
        for i in review_ids:
            for col, code, related in issues[i]:
                w.writerow({"row_id": i, "column": col, "value": rows[i][col],
                            "reason_code": code, "reason": REASONS[code],
                            "related_rows": related, **rows[i]})

    # ---- self-check: every difference must be logged, every row accounted for
    problems = []
    logged = {(e["row_id"], e["column"]): e["new_value"] for e in log}
    for i in kept:
        for c in columns:
            if rows[i][c] != originals[i - 1][c] and logged.get((i, c)) != rows[i][c]:
                problems.append(f"row {i} column {c} changed but not logged")
    for i in removed:
        if (i, "*") not in logged:
            problems.append(f"row {i} removed but not logged")
    total = len(clean_ids) + len(review_ids) + len(removed)
    if total != len(originals):
        problems.append(f"row count mismatch: {len(originals)} in, {total} accounted for")

    # ---- summary
    print(f"Input rows:        {len(originals)}")
    print(f"Clean rows:        {len(clean_ids)}")
    print(f"Rows to review:    {len(review_ids)}")
    print(f"Duplicates removed:{len(removed):>3}")
    print(f"Logged changes:    {len(log)}")
    print("Changes by rule:   " + ", ".join(f"{k} {v}" for k, v in sorted(
        Counter(r for e in log for r in e["rule_id"].split("+")).items())))
    print("Review by reason:  " + ", ".join(f"{k} {v}" for k, v in sorted(
        Counter(code for i in review_ids for _, code, _ in issues[i]).items())))
    if problems:
        print("SELF-CHECK FAILED:")
        for p in problems:
            print("  - " + p)
        return 1
    print("SELF-CHECK PASSED: every change is logged and every row is accounted for.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Clean a messy CSV using a rule file.")
    ap.add_argument("input")
    ap.add_argument("--rules", default="rules.yaml")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    sys.exit(run(a.input, a.rules, a.out))
