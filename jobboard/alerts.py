#!/usr/bin/env python3
"""
jobboard/alerts.py — envoie une alerte email par abonné pour les offres
apparues depuis le dernier envoi.

Lancé par pipeline.sh, après build.py (qui écrit site/jobs.json, chaque
offre portant déjà `first_seen` / `is_new`).

Abonnés : EmailOctopus (liste "Alertes sudtechjobs"), champs perso
`Search` / `Searchlabel` remplis par jobboard/worker/subscribe.js quand
quelqu'un clique "🔔 Recevoir par email" sur le site. EmailOctopus est la
base de consentement/désinscription, pas l'expéditeur : chaque abonné a une
recherche différente donc un contenu différent, ce que fait un outil de
campagne (un seul contenu envoyé à tout le monde), pas un service
d'alertes personnalisées. L'envoi passe par Resend (transactionnel).

État (jobboard/data/alerts_state.json, committé par la CI comme seen.json) :
  {"last_run_date": "YYYY-MM-DD", "last_run_at": "<iso>"}
  -> au plus un envoi réel par jour, même si le pipeline tourne plusieurs
     fois (push, relance manuelle). last_run_at fixe la fenêtre "nouveau
     depuis" du prochain run.

Secrets attendus en env (GitHub Actions) :
  EMAILOCTOPUS_API_KEY, EMAILOCTOPUS_LIST_ID   (déjà utilisés par le worker)
  RESEND_API_KEY
  ALERT_FROM   (optionnel, défaut "sudtechjobs <alertes@sudtechjobs.com>")

Usage :
  python3 jobboard/alerts.py             # envoie (sauf si déjà fait aujourd'hui)
  python3 jobboard/alerts.py --dry-run   # affiche ce qui serait envoyé, n'envoie rien,
                                          # ne touche pas l'état. Sans clé EmailOctopus
                                          # en env, utilise un contact d'exemple —
                                          # testable sans aucun compte.
  python3 jobboard/alerts.py --force     # ignore le garde-fou "une fois par jour"
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
JOBS_PATH = ROOT / "site" / "jobs.json"
STATE_PATH = ROOT / "data" / "alerts_state.json"
SITE_URL = "https://sudtechjobs.com"
FIRST_RUN_WINDOW_HOURS = 48  # au tout premier envoi, pas tout le backlog

EO_API_KEY = os.environ.get("EMAILOCTOPUS_API_KEY", "")
EO_LIST_ID = os.environ.get("EMAILOCTOPUS_LIST_ID", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ALERT_FROM = os.environ.get("ALERT_FROM", "sudtechjobs <alertes@sudtechjobs.com>")

EXAMPLE_CONTACT = {
    "status": "subscribed",
    "email_address": "exemple@sudtechjobs.com",
    "fields": {"Search": "cat=eng&city=Nice", "Searchlabel": "Dev · Nice"},
}


# --- filtre : port fidèle de apply() dans site/index.html, pour que les
# alertes matchent exactement ce que le site aurait montré. ---

def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def haystack(j):
    parts = [
        j.get("title"), j.get("company"), j.get("city"),
        " ".join(j.get("cities") or []), j.get("profession"), j.get("contract"),
        " ".join(j.get("stack") or []), j.get("description_excerpt"), j.get("profile_excerpt"),
    ]
    return norm(" ".join(p for p in parts if p))


def parse_search(qs):
    p = parse_qs(qs or "", keep_blank_values=False)
    g = lambda k: set(p.get(k, []))
    return {
        "q": (p.get("q") or [""])[0],
        "cats": g("cat"), "cities": g("city"), "stacks": g("tech"),
        "contracts": g("contract"), "exps": g("exp"),
        "remote": (p.get("remote") or [""])[0] == "1",
        "only_new": (p.get("new") or [""])[0] == "1",
    }


def matches(job, f):
    if f["only_new"] and not job.get("is_new"):
        return False
    if f["cats"] and job.get("category") not in f["cats"]:
        return False
    if f["cities"]:
        jc = {job.get("city"), *(job.get("cities") or [])}
        if not (jc & f["cities"]):
            return False
    if f["stacks"]:
        js = {norm(s) for s in (job.get("stack") or [])}
        if not any(norm(s) in js for s in f["stacks"]):
            return False
    if f["contracts"] and (job.get("contract") or "") not in f["contracts"]:
        return False
    if f["exps"] and (job.get("experience") or "") not in f["exps"]:
        return False
    if f["remote"]:
        r = norm(" ".join(filter(None, [job.get("remote_detail"), job.get("remote")])))
        if "ponctuel" in r:
            return False
        if not re.search(r"remote|hybride|t.l.travail", r):
            return False
    q = [w for w in norm(f["q"]).split() if w]
    if q:
        h = haystack(job)
        if not all(w in h for w in q):
            return False
    return True


# --- offres nouvelles depuis <since> ---

def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_new_jobs(since_iso):
    feed = json.loads(JOBS_PATH.read_text())
    since = parse_iso(since_iso)
    out = []
    for j in feed.get("jobs", []):
        fs = parse_iso(j.get("first_seen"))
        if fs and (since is None or fs > since):
            out.append(j)
    return out


# --- état ---

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


# --- EmailOctopus : lecture des abonnés (pas d'envoi) ---

def eo_get_contacts():
    contacts, cursor = [], None
    while True:
        url = f"https://api.emailoctopus.com/lists/{EO_LIST_ID}/contacts?limit=100"
        if cursor:
            url += f"&starting_after={cursor}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {EO_API_KEY}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read())
        batch = payload.get("data", [])
        contacts.extend(batch)
        cursor = ((payload.get("paging") or {}).get("next") or {}).get("starting_after")
        if not cursor or not batch:
            break
    return contacts


# --- rendu + envoi ---

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_email(jobs, search_label):
    rows = "".join(f"""
      <tr><td style="padding:14px 0;border-bottom:1px solid #DCE4EC">
        <div style="font:600 15px 'Bricolage Grotesque',sans-serif;color:#16303F">
          <a href="{SITE_URL}/offre/{j.get('slug', '')}.html" style="color:#16303F;text-decoration:none">{esc(j.get('title'))}</a>
        </div>
        <div style="font-size:13px;color:#5F7488;margin-top:3px">{esc(j.get('company'))}{' · ' + esc(j['city']) if j.get('city') else ''}</div>
      </td></tr>""" for j in jobs)
    n = len(jobs)
    return f"""<!doctype html><html><body style="margin:0;background:#EDF2F6;font-family:'Hanken Grotesk',ui-sans-serif,sans-serif;color:#16303F">
<table role="presentation" width="100%" style="max-width:560px;margin:0 auto;padding:28px 20px">
  <tr><td style="font:700 21px 'Bricolage Grotesque',sans-serif">sudtechjobs<span style="color:#F2A63B">.</span></td></tr>
  <tr><td style="padding:16px 0 4px;font-size:14px;color:#5F7488">
    {n} nouvelle{'s' if n > 1 else ''} offre{'s' if n > 1 else ''} pour <b style="color:#16303F">{esc(search_label)}</b>
  </td></tr>
  <tr><td><table role="presentation" width="100%">{rows}</table></td></tr>
  <tr><td style="padding-top:24px;font-size:11.5px;color:#8DA2B2;line-height:1.6">
    Tu reçois cet email parce que tu t'es inscrit·e à une alerte sur sudtechjobs.com pour cette recherche.
    Pour te désabonner, écris à <a href="mailto:hello@sudtechjobs.com" style="color:#2C6C9E">hello@sudtechjobs.com</a>.
  </td></tr>
</table></body></html>"""


def send_email(to_addr, subject, html):
    body = json.dumps({"from": ALERT_FROM, "to": [to_addr], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="affiche sans envoyer ni changer l'état")
    ap.add_argument("--force", action="store_true", help="ignore le garde-fou une-fois-par-jour")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date().isoformat()
    state = load_state()
    # garantit que le fichier existe dès ce run, même si on s'arrête plus bas
    # (secrets manquants...) -- la CI l'ajoute au commit sans se soucier du cas
    # "premier run, fichier encore absent".
    if not args.dry_run and not STATE_PATH.exists():
        save_state(state)
    if not args.dry_run and not args.force and state.get("last_run_date") == today:
        print(f"alerts: déjà envoyé aujourd'hui ({today}) — on saute.")
        return

    since_iso = state.get("last_run_at")
    if since_iso is None:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_WINDOW_HOURS)).isoformat()
    new_jobs = load_new_jobs(since_iso)
    print(f"alerts: {len(new_jobs)} offre(s) apparue(s) depuis {since_iso}")

    if args.dry_run and not (EO_API_KEY and EO_LIST_ID):
        print("alerts: pas de clé EmailOctopus dans l'environnement — dry-run avec un contact d'exemple.")
        contacts = [EXAMPLE_CONTACT]
    elif not (EO_API_KEY and EO_LIST_ID):
        print("alerts: EMAILOCTOPUS_API_KEY / EMAILOCTOPUS_LIST_ID manquants — abandon.", file=sys.stderr)
        return
    else:
        try:
            contacts = eo_get_contacts()
        except urllib.error.HTTPError as e:
            detail = e.read()[:400]
            print(f"alerts: échec lecture des contacts EmailOctopus ({e.code} {e.reason}): {detail}",
                  file=sys.stderr)
            return
        except Exception as e:
            print(f"alerts: échec lecture des contacts EmailOctopus: {e}", file=sys.stderr)
            return
    print(f"alerts: {len(contacts)} contact(s)")

    if not args.dry_run and not RESEND_API_KEY:
        print("alerts: RESEND_API_KEY manquant — abandon (rien envoyé, état non modifié).", file=sys.stderr)
        return

    sent = skipped_empty = skipped_no_search = errors = 0
    for c in contacts:
        if c.get("status") != "subscribed":
            continue
        email = c.get("email_address")
        fields = c.get("fields") or {}
        qs = fields.get("Search")
        if qs is None:
            skipped_no_search += 1
            continue
        label = fields.get("Searchlabel") or "toutes les offres"
        matched = [j for j in new_jobs if matches(j, parse_search(qs))]
        if not matched:
            skipped_empty += 1
            continue
        n = len(matched)
        subject = f"{n} nouvelle{'s' if n > 1 else ''} offre{'s' if n > 1 else ''} · {label}"
        if args.dry_run:
            print(f"  [dry-run] -> {email} : {n} offre(s) [{label}]")
            for j in matched[:5]:
                print(f"      - {j.get('title')} @ {j.get('company')}")
        else:
            try:
                send_email(email, subject, render_email(matched, label))
                sent += 1
            except urllib.error.HTTPError as e:
                print(f"  ERREUR envoi {email}: {e.code} {e.read()[:300]}", file=sys.stderr)
                errors += 1
            except Exception as e:
                print(f"  ERREUR envoi {email}: {e}", file=sys.stderr)
                errors += 1

    print(f"alerts: {sent} envoyé(s), {skipped_empty} sans nouveauté, "
          f"{skipped_no_search} sans recherche enregistrée, {errors} erreur(s)")

    if not args.dry_run:
        save_state({"last_run_date": today, "last_run_at": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    main()
