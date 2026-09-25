#!/usr/bin/env python3
"""Historical stats for the job feed — daily snapshots + month backfill from git.

`jobboard/data/jobs.json` is overwritten on every CI run but git keeps a full
snapshot at every commit, so the offer history already exists — it's just
buried in git log instead of a queryable file. This script surfaces it:

    python jobboard/history.py record              append today's aggregate
                                                     to data/history.jsonl
                                                     (idempotent: reruns same
                                                     day replace that line)

    python jobboard/history.py backfill [--since YYYY-MM-DD]
                                                     walk every git commit that
                                                     touched data/jobs.json,
                                                     rebuild one history.jsonl
                                                     line per day, and print a
                                                     month-to-date summary of
                                                     every DISTINCT offer seen
                                                     (union across all daily
                                                     snapshots — jobs.json
                                                     alone only shows what's
                                                     live *today*, so this is
                                                     the only accurate way to
                                                     count "every offer this
                                                     month", including ones
                                                     that were filled/expired
                                                     and dropped off the feed)

Output: jobboard/data/history.jsonl, one JSON object per calendar day:
    {"date": "2026-09-22", "count": 809, "new_count": 452,
     "by_category": {...}, "by_source": {...}}
"""
import argparse
import collections
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(HERE, "data")
JOBS_REL = "jobboard/data/jobs.json"
HISTORY_PATH = os.path.join(DATA, "history.jsonl")

# Rough hub buckets for the monthly report — reuses the same PACA towns build.py
# filters on, just grouped into the handful of hubs people actually ask about.
HUBS = [
    ("Marseille", ("marseille", "aubagne", "gardanne", "gemenos", "la ciotat",
                    "vitrolles", "marignane", "fos-sur-mer", "fos sur mer", "istres",
                    "martigues", "salon-de-provence", "salon de provence")),
    ("Aix-en-Provence", ("aix-en-provence", "aix en provence", "meyreuil",
                          "venelles", "bouc-bel-air", "bouc bel air", "rousset",
                          "pertuis")),
    ("Sophia Antipolis / Nice", ("sophia", "antipolis", "valbonne", "biot",
                                   "nice", "carros")),
    ("Cannes / Antibes / Grasse", ("cannes", "antibes", "mougins", "grasse")),
    ("Toulon / Var", ("toulon", "six-fours", "six fours", "hyeres", "hyères",
                       "draguignan", "frejus", "fréjus", "la seyne")),
    ("Avignon / Vaucluse", ("avignon",)),
]


def _bucket_hub(city):
    c = (city or "").lower()
    for label, keywords in HUBS:
        if any(k in c for k in keywords):
            return label
    return "Autre PACA"


def _run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _snapshot_from_jobs(doc):
    jobs = doc.get("jobs", [])
    by_hub = collections.Counter(_bucket_hub(j.get("city")) for j in jobs)
    by_city = collections.Counter(j.get("city") for j in jobs if j.get("city"))
    return {
        "count": doc.get("count", len(jobs)),
        "new_count": doc.get("new_count"),
        "by_category": doc.get("by_category", {}),
        "by_source": doc.get("by_source", {}),
        "by_hub": dict(by_hub),
        "by_city": dict(by_city),
    }


def cmd_record(_args):
    path = os.path.join(DATA, "jobs.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    generated_at = doc.get("generated_at", "")
    date = generated_at[:10] if generated_at else None
    if not date:
        sys.exit("jobs.json has no generated_at — nothing to record")

    row = {"date": date, **_snapshot_from_jobs(doc)}

    rows = _load_history()
    rows = [r for r in rows if r["date"] != date]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    _write_history(rows)
    print("recorded %s: %d offers live (%s new)" % (date, row["count"], row["new_count"]))


def _load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    rows = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _write_history(rows):
    os.makedirs(DATA, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def cmd_backfill(args):
    log = _run(["git", "log", "--format=%H|%aI", "--reverse", "--", JOBS_REL]).strip()
    commits = [line.split("|", 1) for line in log.splitlines() if line]
    if args.since:
        commits = [(h, d) for h, d in commits if d[:10] >= args.since]
    if not commits:
        sys.exit("no commits found touching %s" % JOBS_REL)

    print("walking %d commits from %s to %s ..." % (
        len(commits), commits[0][1][:10], commits[-1][1][:10]), file=sys.stderr)

    daily_last = {}          # date -> (commit_datetime, snapshot dict)
    offers = {}               # job id -> {first_seen, last_seen, category, source, city, company, title}

    for i, (sha, iso_date) in enumerate(commits):
        date = iso_date[:10]
        try:
            raw = _run(["git", "show", "%s:%s" % (sha, JOBS_REL)])
            doc = json.loads(raw)
        except (subprocess.CalledProcessError, ValueError):
            continue

        snap = _snapshot_from_jobs(doc)
        prev = daily_last.get(date)
        if prev is None or iso_date >= prev[0]:
            daily_last[date] = (iso_date, snap)

        for j in doc.get("jobs", []):
            jid = j.get("id")
            if not jid:
                continue
            rec = offers.get(jid)
            if rec is None:
                offers[jid] = {
                    "first_seen": date,
                    "last_seen": date,
                    "category": j.get("category"),
                    "source": j.get("source"),
                    "city": j.get("city"),
                    "company": j.get("company"),
                    "title": j.get("title"),
                }
            else:
                rec["last_seen"] = date
                rec["category"] = j.get("category") or rec["category"]
                rec["city"] = j.get("city") or rec["city"]
        if (i + 1) % 10 == 0:
            print("  ...%d/%d commits (%s)" % (i + 1, len(commits), date), file=sys.stderr)

    rows = [{"date": d, **snap} for d, (_, snap) in sorted(daily_last.items())]
    _write_history(rows)
    print("wrote %d daily rows to %s" % (len(rows), HISTORY_PATH), file=sys.stderr)

    _print_month_summary(offers, rows)


def _print_month_summary(offers, rows):
    total = len(offers)
    by_cat = collections.Counter(o["category"] or "non classé" for o in offers.values())
    by_src = collections.Counter(o["source"] or "?" for o in offers.values())
    by_hub = collections.Counter(_bucket_hub(o["city"]) for o in offers.values())

    print("\n=== Offres distinctes sur la période (%s -> %s) ===" % (
        rows[0]["date"], rows[-1]["date"]))
    print("Total offres distinctes vues : %d" % total)
    print("(pour comparaison, snapshot du dernier jour : %d offres live)" % rows[-1]["count"])

    print("\nPar catégorie :")
    for cat, n in by_cat.most_common():
        print("  %-15s %4d" % (cat, n))

    print("\nPar bassin :")
    for hub, n in by_hub.most_common():
        print("  %-28s %4d" % (hub, n))

    print("\nPar source (top 10) :")
    for src, n in by_src.most_common(10):
        print("  %-15s %4d" % (src, n))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("record", help="append today's snapshot from data/jobs.json")

    bf = sub.add_parser("backfill", help="rebuild history.jsonl from git log + print summary")
    bf.add_argument("--since", help="only commits from this date (YYYY-MM-DD) onward")

    args = p.parse_args()
    {"record": cmd_record, "backfill": cmd_backfill}[args.cmd](args)


if __name__ == "__main__":
    main()
