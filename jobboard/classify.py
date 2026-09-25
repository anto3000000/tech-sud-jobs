"""Tech / Data / Product job classifier.

WTTJ aggregates thousands of PACA listings but only ~12 % carry a real
`profession` taxonomy, so we cannot trust their facet. This module decides
whether a posting belongs on a *tech* job board from its title (+ the
profession / sector strings when present).

`classify(title, profession=None, sector=None)` -> one of:
    "eng"            plain software dev (full/back/front, embedded, mobile, tech lead…)
    "devops"         DevOps / SRE / cloud / platform / infra / sysadmin
    "cybersecurite"  security engineering, pentest, SOC, RSSI
    "architecte"     software/solutions/CTO-level architecture
    "qa"             QA / test automation
    "product"        product management, product ownership
    "design"         product & UX/UI design
    "data"           data / ML / analytics / BI
    "tech-adjacent"  IT support, sysadmin, tech recruiter, technical sales...
    None             not a tech role

`is_tech(...)` -> bool  (True for the core buckets, and optionally adjacent)
"""
import re
import unicodedata

def _norm(s):
    if isinstance(s, (list, tuple)):
        s = " ".join(str(x) for x in s)
    elif s is None:
        s = ""
    else:
        s = str(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


# order matters: first bucket that matches wins
_RULES = [
    ("data", [
        r"\bdata\b", r"\bdonnees\b", r"data (scientist|engineer|analyst|architect)",
        r"machine learning", r"\bml\b", r"\bmlops\b", r"ml ?engineer",
        r"(ingenieur|engineer|dev|developp|expert|lead|chercheur|scientist)[^,]*\b(ai|ia)\b",
        r"\b(ai|ia)\b[^,]*(engineer|ingenieur|developp|scientist|research|platform)",
        r"intelligence artificielle", r"\bnlp\b", r"computer vision", r"\bllm\b", r"\bgenai\b",
        r"\bbi\b", r"business intelligence", r"analytics", r"\betl\b", r"dbt\b",
        r"\bbig ?data\b", r"data ?viz", r"statisticien", r"\bdatawarehouse\b",
        r"business analyst", r"business analyste", r"\bit business analys[ie]s",
        r"operations research", r"recherche operationnelle",
    ]),
    ("product", [
        r"product manager", r"product owner", r"\bpo\b", r"\bpm\b(?![a-z])",
        r"chef de produit (digital|web|numerique|tech)", r"head of product",
        r"product ops", r"product lead", r"lead product", r"\bcpo\b",
        r"responsable produit", r"product analyst", r"growth product",
        r"product builder", r"product strategist", r"product operations",
    ]),
    ("design", [
        r"\bux\b", r"\bui\b", r"ux/ui", r"ui/ux", r"product designer",
        r"design system", r"designer (web|produit|digital|d.interface)",
        r"motion designer", r"\bux researcher\b", r"design lead", r"head of design",
    ]),
    ("devops", [
        r"\bsre\b", r"\bdevops\b", r"platform engineer", r"cloud engineer",
        r"site reliability", r"infrastructure engineer", r"\bdevsecops\b",
        r"\bsysadmin\b", r"administrateur (systeme|reseau|base de donnees|"
        r"infrastructure|cloud|linux|windows)",
        r"\bdba\b", r"ing[eé]nieur(e)? (cloud|devops)",
        r"\bkubernetes\b", r"\bterraform\b", r"\bansible\b",
    ]),
    ("cybersecurite", [
        r"cyber ?securit", r"cybersecurit", r"security engineer", r"pentest",
        r"\brssi\b", r"analyste (soc|cyber|securite)", r"\bsoc analyst\b",
        r"\bcyber\b", r"ingenieur securite", r"ing[eé]nieur(e)? (securite|cyber)",
    ]),
    ("architecte", [
        r"architecte (logiciel|technique|si|cloud|solution|systeme)",
        r"software architect", r"\bcto\b",
    ]),
    ("qa", [
        r"\bqa\b", r"quality assurance", r"test automation", r"\bqa engineer\b",
        r"ingenieur validation", r"ing[eé]nieur(e)? (test|qa)", r"\btesteur\b",
    ]),
    ("eng", [
        r"developp(eur|euse|ement)", r"\bdev\b", r"\bdeveloper\b", r"software engineer",
        # NB: "etude"/"d'etude"/"systeme"/"reseau" are deliberately NOT in this
        # alternation — "études"/"système" are generic role words used across
        # every engineering discipline (civil, nucléaire, avionique, électrique…),
        # not just software; "Ingénieur Études Génie Civil" was matching here.
        r"ing[eé]nieur(e)? (logiciel|informatique|r&d|embarque|full ?stack|back|front|web)",
        r"software (engineer|developer)", r"full ?stack", r"back ?end",
        r"front ?end",
        r"embedded", r"embarque(?!ment)", r"firmware", r"\bfpga\b", r"\bvlsi\b", r"\basic\b",
        r"electronique", r"\biot\b", r"robotique", r"\brobotics\b",
        r"tech ?lead", r"lead tech", r"lead (dev|developer|engineer|technique)",
        r"staff engineer", r"principal engineer",
        r"scrum master", r"agile coach",
        r"integration engineer", r"\bapi\b engineer", r"blockchain",
        r"\bweb3\b", r"game (developer|engineer|programmer)", r"gameplay",
        r"mobile (developer|engineer)", r"\bios\b (developer|engineer)",
        r"\bandroid\b (developer|engineer)",
        r"ingenieur developpement", r"ingenieur integration",
        r"\bhardware\b", r"hardware engineer", r"\brtl\b design",
        # explicit language / stack tokens — a title can be just "Lead Python / Django"
        r"\bpython\b", r"\bjava\b", r"\brust\b", r"\bgolang\b", r"\bkotlin\b",
        r"\bscala\b", r"\belixir\b", r"\bc\+\+", r"\bc#", r"\.net\b", r"\bphp\b",
        r"\bruby\b", r"\btypescript\b", r"\breact\b", r"\bangular\b", r"\bvue\.?js\b",
        r"\bnode\.?js\b", r"\bdjango\b", r"\bsymfony\b", r"\blaravel\b",
        r"spring boot", r"ruby on rails",
    ]),
    ("tech-adjacent", [
        r"support (technique|informatique|it|n[12123]|applicatif)",
        r"technicien (informatique|support|systeme reseau|systeme|helpdesk|it\b|"
        r"de proximite|infrastructure|micro)",
        r"help ?desk", r"hotline",
        r"technico[- ]commercial (logiciel|it|saas|informatique|software|"
        r"cyber|digital|electroni)",
        r"sales engineer", r"solution(s)? engineer", r"presales", r"avant[- ]vente",
        r"customer success (engineer|manager)", r"implementation (specialist|consultant)",
        r"consultant(e)?( en)? transformation (digitale|numerique)",
        r"consultant (si|erp|sap|salesforce|crm|bi|data|cloud|cyber|it|technique|"
        r"fonctionnel|digital|dynamics|informatique)",
        r"\bmoa\b", r"\bmoe\b", r"assistance maitrise",
        r"chef(fe)? de projet (si\b|it\b|informatique|digital|web|technique|erp|data|"
        r"applicatif|infrastructure|numerique|mobile|logiciel|software|cyber|cloud|"
        r"deploiement|integration|test)",
        r"project manager officer tech", r"\bpmo\b (it|si|tech)",
        r"recruteur (tech|it)", r"talent acquisition (tech|it|engineering)",
        r"tech recruiter", r"it recruiter", r"ingenieur commercial (logiciel|it|"
        r"informatique|saas)", r"deliverabilite", r"seo\b", r"sea\b",
        r"traffic manager", r"growth (hacker|marketer|manager)", r"web ?master",
        r"integrateur web", r"\berp\b", r"administrateur fonctionnel",
        r"analyste programmeur",
    ]),
]

_COMPILED = [(b, [re.compile(p) for p in pats]) for b, pats in _RULES]

# hard negatives: kill common false positives from the "eng" net
_NEG = re.compile(
    r"amenagement urbain|\bvrd\b|hydraulique urbaine|eaux usees|assainissement|"
    r"\bcvc\b|genie climatique|plomberie|"
    r"ingenieur (btp|genie civil|structure|batiment|travaux|thermique|"
    r"hse|qhse|process chimi|methode(s)? industriel|production industriel|"
    r"mecanique(?! embarqu)|hydraulique|geotechni|environnement(?!.*logiciel)|"
    r"acoustique|nucleaire(?!.*(logiciel|data|cyber))|ferroviaire(?!.*(logiciel|"
    r"embarque|data))|maintenance industriel|electrotechni(?!.*embarque)|"
    r"automaticien(?!.*(logiciel|embarque)))|"
    r"technicien (de maintenance(?! informatique)|labo|chimi|qualite(?! logiciel)|"
    r"cvc|frigoriste|electricien|fibre optique|telecom(?! logiciel)|de laboratoire)|"
    # "electronique" (eng net) is a hardware-engineering keyword, not a role —
    # it also fires on supply-chain/support/coordination roles that merely deal
    # in electronic equipment without doing any engineering themselves
    r"supply chain|coordinateur (obsolescence|approvisionnement|logistique)|"
    r"responsable support (produit|client)|"
    r"commercial(?!.*(logiciel|it|saas|informatique|tech|digital))|"
    r"conseiller (de vente|clientele|bancaire)|comptable|assistant(e)? (commercial|"
    r"administratif|de direction|rh)|charge de recrutement(?! tech)|"
    r"employe|serveur (h/f|polyvalent|de restaurant)|cuisinier|patissier|barman|"
    r"vendeur|hote(sse)? |manutentionnaire|preparateur de commande|cariste|"
    r"chauffeur|livreur|agent (de|d.)|garde d.enfant|aide (a domicile|soignant)|"
    r"infirmier|medecin|pharmacien|kinesitherapeute|educateur"
)


# dev/data tooling that, when it shows up in a WTTJ `tools` list, is decisive
_STACK_HINT = re.compile(
    r"\b(python|java|kotlin|scala|rust|golang|\.net|c\+\+|c#|php|ruby|typescript|"
    r"javascript|react|angular|vue|node|django|symfony|laravel|spring|"
    r"kubernetes|docker|terraform|ansible|jenkins|gitlab ci|github actions|"
    r"aws|azure|gcp|linux|postgres|mysql|mongodb|redis|kafka|spark|airflow|dbt|"
    r"snowflake|databricks|tensorflow|pytorch|scikit|pandas|power bi|tableau|"
    r"elasticsearch|grafana|prometheus|api rest|graphql|flutter|swift|"
    r"unreal|unity|solidity)\b", re.I)


_AI_TITLE = re.compile(
    r"\b(ai|ia)\b.*\b(analyst|analyste|engineer|ingenieur|architect|architecte|"
    r"scientist|developer|entwickler|algorithm\w*|knowledge|connaissances|"
    r"predictive|solutions?)\b|"
    r"\b(analyst|analyste|engineer|ingenieur|architect|architecte|scientist|"
    r"developer|chef(fe)? de projets?|consultant\w*)\b.*\b(ai|ia)\b")
_AI_NEG = re.compile(r"pedagogi|sourcing|outbound|growth|formateur|formation|"
                     r"gestion de projet|qualite|hse|sse\b|finance|juridique")


_GENDER_SUFFIX = re.compile(r"\b(\w+?)[.](e|se|rice|euse|trice|ere|eure|fe)\b")
_GENDER_SUFFIX_PARENS = re.compile(r"\((e|se|rice|euse|trice|ere|eure|fe)\)")


def classify(title, profession=None, sector=None, stack=None):
    t = _norm(title)
    # French inclusive writing ("Consultant.e", "Développeur.se",
    # "Coordinateur(rice)") glues a gender suffix onto the word with "."/"()"
    # rather than a space — every `word\w*\s+...` pattern below stops dead at
    # that punctuation and silently misses the title (e.g. "Consultant.e en
    # management" never matched the "consultant...en management" carve-out).
    # Collapse it to the masculine base form once, up front, instead of
    # patching every downstream regex individually. "·" needs no handling —
    # _norm's ascii-only encode already drops it (merging into one word).
    t = _GENDER_SUFFIX_PARENS.sub("", t)
    t = _GENDER_SUFFIX.sub(r"\1", t)
    prof = _norm(profession)
    stack_txt = _norm(stack if not isinstance(stack, str) else [stack])
    stack_hits = len(set(_STACK_HINT.findall(stack_txt))) if stack_txt else 0

    # `blob` feeds the per-bucket keyword net below: TITLE ONLY. Neither
    # `profession`/`department` nor `stack` is safe to blend in as blanket
    # search text — both are company-supplied metadata, not a description of
    # this specific job: Eurofins' SmartRecruiters department is "Quality
    # Assurance" for food-safety auditor roles (false-positived via the
    # `quality assurance` pattern), and a company's WTTJ tools list is often
    # shared across a whole team — Eskimoz tags its SEO/marketing postings
    # with "React"/"Python" (shared tooling), which used to hit the bare
    # `\breact\b`/`\bpython\b` eng patterns before the title-based
    # tech-adjacent bucket ever got a chance to match. Both signals are still
    # used below, but only in the two places that treat them as a deliberate,
    # narrow signal (WTTJ's real profession taxonomy, the tech-recruiter
    # context check, and the last-resort stack_hits>=2 fallback) — never as
    # blanket search text against all bucket patterns.
    blob = t
    prof_blob = blob + (" || " + prof if prof else "")

    # profession taxonomy is authoritative when WTTJ actually set it
    if prof in ("data", "data engineering", "data science", "data analysis",
                "machine learning", "business intelligence"):
        return "data"
    if "product management" in prof or prof in ("product management", "product"):
        return "product"
    if prof in ("ui design", "ux design", "ux/ui design", "product design"):
        return "design"

    # "business developer" / "business development" / "biz dev" / BDR — and
    # its French idiom "développeur commercial" — is a sales role, not
    # engineering, even though "developer" / "developp" matches the eng net
    if re.search(r"\bbusiness\s+develop\w*|\bbiz\s*dev\b|\bbusiness\s+dev\b|"
                 r"\bbdr\b|\bbusiness development (manager|representative|rep)\b|"
                 r"developpeu(r|se) commercial", t):
        return None

    # "electronique" (eng net) is a hardware-engineering keyword, not a role —
    # a supply-chain/support/coordination job that merely deals in electronic
    # equipment isn't itself engineering. Return early: the _NEG block below
    # has its own "still allow if an eng/data pattern matches" fallback that
    # would otherwise let these back in through that same bare keyword.
    if re.search(r"supply chain|coordinateur (obsolescence|approvisionnement|"
                 r"logistique)|responsable support (produit|client)", t):
        return None

    # marketing/ad-tech "analytics & tracking" (GA/GTM/Ads implementation for
    # a marketing agency, e.g. Eskimoz) reads like a data-analytics title on
    # keywords alone but isn't data engineering/analysis
    if re.search(r"analytics\s*/\s*tracking|analytics\s+et\s+tracking", t):
        return None

    # tourism/travel "développement produit" ("Chargé Développement Produit
    # Destination") reads exactly like a software product-dev title on
    # keywords alone ("produit" is in `_dev_tech` below) but is a completely
    # different field — catch it before the generic carve-out has a chance
    # to wave it through.
    if re.search(r"developp?ement\s+produit\w*\s+(destination|touristique)|"
                 r"produit\w*\s+touristique", t):
        return None

    # French "développement" trips the eng net: "chargé de développement
    # commercial / RH / foncier / stratégique / fournisseurs…" are sales / HR
    # / real-estate / procurement roles, not software. Kill them unless the
    # title also names a real tech object (logiciel, produit, web, API, data…).
    _dev_tech = re.compile(r"logiciel|software|produit|web|mobile|application|"
                           r"back ?end|front ?end|full ?stack|\bapi\b|plateforme|"
                           r"\bdata\b|informatique|numerique|\br ?& ?d\b|\brd\b|"
                           r"embarque|firmware|\bsi\b")
    if re.search(r"developp?ement\s+(commercial|rh\b|des ventes|de la clientele|"
                 r"foncier|immobilier|de portefeuille|des affaires|international|"
                 r"partenariat|strateg|durable|economique|territor|local|rural|"
                 r"des ressources humaines|de l.?activite|reseau|fournisseur(s)?|"
                 r"des achats)", t) \
            and not _dev_tech.search(t):
        return None
    # same role-titles, but phrased without "de" ("Responsable Développement
    # Grands Comptes" rather than "Responsable de Développement…") — the
    # topic word isn't in the explicit list above, so fall back to: any of
    # these role words directly ahead of "développement", with no tech object
    # named anywhere in the title, is presumed non-tech.
    if re.search(r"\b(charge[e]?|responsable|assistant[e]?|adjoint[e]?|directeur|"
                 r"directrice|manager|conseiller[e]?)\b.{0,25}\b(?:de\s+)?developp?ement\b", t) \
            and not _dev_tech.search(t):
        return None

    # management/strategy consulting ON a tech topic (Wavestone-style —
    # "Consultant.e en management - Cybersécurité") is advisory, not hands-on
    # engineering — route it to tech-adjacent before the bare "cybersecurite"
    # / "data" / … keywords elsewhere grab it as eng/cybersecurite/data.
    # NB: [\w.()]* (not \w*) after "consultant"/"conseil" — inclusive-writing
    # gender suffixes ("Consultant.e", "Consultant(e)") aren't \w, so a bare
    # \w* stops dead at the punctuation and silently misses the title.
    if re.search(r"consultant[\w.()]*\s+en\s+management|conseil[\w.()]*\s+en\s+management|"
                 r"consultant[\w.()]*\s+en\s+strategie|management\s+consult", t):
        return "tech-adjacent" if re.search(
            r"cyber|securit|\bdata\b|digital|\bia\b|\bai\b|informatique|"
            r"numerique|technolog", t) else None

    # recruiter / sourcer roles are people-ops, never eng/data even when the
    # copy is stuffed with "AI" / "engineering"
    if re.search(r"\b(recruit(er|eur|euse)|sourcer|talent acquisition|"
                 r"charge[e]? de recrutement|talent partner|hr business partner)\b", t):
        return "tech-adjacent" if re.search(
            r"\b(tech|it|engineering|software|dev|data|saas|digital)\b", prof_blob) else None

    if _NEG.search(t) and not re.search(r"logiciel|software|\bdata\b|cyber|devops|"
                                        r"full ?stack|back ?end|front ?end", prof_blob):
        # still allow if an explicit dev/data bucket also fires
        for bucket, pats in _COMPILED:
            if bucket in ("eng", "data", "devops", "cybersecurite", "architecte", "qa") \
                    and any(p.search(blob) for p in pats):
                return bucket
        return None

    # "Business Analyst / Product Owner" hybrid -> the product half wins
    # (plain "business analyst" is data, see the data bucket)
    if re.search(r"business analyst", t) and re.search(r"product (owner|manager)", t):
        return "product"

    for bucket, pats in _COMPILED:
        if any(p.search(blob) for p in pats):
            return bucket

    # vague title ("Consultant", "Ingénieur", "Expert F/H") but the WTTJ tools
    # list is unmistakably a dev/data stack -> trust it
    # "IA" / "AI" next to a role word ("Analyste IA", "AI for Knowledge
    # Management", "Chef de projets IA"). Title only: the profession string
    # ("Data & AI") is a department label, and this runs last so it never
    # steals a title that already has a better bucket.
    if _AI_TITLE.search(t) and not _AI_NEG.search(t):
        return "data"
    if stack_hits >= 2 and not _NEG.search(t):
        data_tools = re.search(r"\b(spark|airflow|dbt|snowflake|databricks|tensorflow|"
                               r"pytorch|scikit|pandas|power bi|tableau|kafka)\b", stack_txt)
        return "data" if data_tools and not _STACK_HINT.search(t) else "eng"
    return None


CORE = {"eng", "devops", "cybersecurite", "architecte", "qa", "product", "design", "data"}


def is_tech(title, profession=None, sector=None, stack=None, include_adjacent=True):
    c = classify(title, profession, sector, stack)
    if c is None:
        return False
    return c in CORE or (include_adjacent and c == "tech-adjacent")


if __name__ == "__main__":
    import sys
    tests = [
        "Développeur Full Stack JS", "Data Scientist H/F", "Product Manager",
        "Product Designer UX/UI", "Ingénieur DevOps", "Analyste Cybersécurité",
        "Ingénieur Génie Civil", "Commercial terrain", "Serveur H/F",
        "Chef de projet SI", "Technico-commercial logiciel", "Comptable",
        "Ingénieur Développement Simulations", "Alternance Développeur Web",
        "Consultant SAP", "Technicien de maintenance industrielle",
        "Business Analyst", "Lead Tech", "Scrum Master", "SRE",
        "Business Developer", "Business Developer Software", "Biz Dev",
        "Business Development Manager", "Business Analyst", "Business Analyst SAP",
        "Business Analyst / Product Owner",
        "Ingénieur systèmes embarqués", "Directeur Commercial",
        # "développement" false positives — all expected None
        "Chargé de Développement Commercial F/H",
        "Responsable Développement Stratégique F/H",
        "Responsable Développement Foncier (H/F)",
        "Chargé(e) de développement RH",
        "Chargé(e) de Développement Commercial & Partenariats - Stage - Marseille - H/F",
        "Développement commercial, apport d'affaires",
        "Chargé de développement territorial",
        # …but these stay eng / tech
        "Ingénieur Développement Simulations",
        "Ingénieur développement logiciel",
        "Responsable développement produit",
        "Chargé de développement d'applications web",
        # student roles previously dropped (title in English / cyber / AI)
        "Internship SOC ANALYST - CYBER DEFENSE CENTER",
        "Internship - Cyber Operational Dashboard",
        "Internship - Group IT Business Analysis",
        "Stage - IA pour la Gestion des Connaissances",
        "Internship Operations Research Engineer",
        "Consultante / Consultant en transformation digitale - Stage",
        # …and things the new rules must NOT catch (expected None)
        "Senior Manager Finance Business Analysis",
        "Accompagnateur Pédagogique - IA",
        "STAGE 2027 - Gestion de Projet IA & Innovation Qualité",
    ]
    for t in (sys.argv[1:] or tests):
        print("  %-45s -> %s" % (t, classify(t)))
