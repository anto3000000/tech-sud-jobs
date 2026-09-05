"""Tech / Data / Product job classifier.

WTTJ aggregates thousands of PACA listings but only ~12 % carry a real
`profession` taxonomy, so we cannot trust their facet. This module decides
whether a posting belongs on a *tech* job board from its title (+ the
profession / sector strings when present).

`classify(title, profession=None, sector=None)` -> one of:
    "eng"      software / infra / data / security / QA / hardware-embedded
    "product"  product management, product ownership
    "design"   product & UX/UI design
    "data"     data / ML / analytics / BI
    "tech-adjacent"  IT support, sysadmin, tech recruiter, technical sales...
    None       not a tech role

`is_tech(...)` -> bool  (True for eng/product/design/data, and optionally adjacent)
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
    ("eng", [
        r"developp(eur|euse|ement)", r"\bdev\b", r"\bdeveloper\b", r"software engineer",
        r"ing[eé]nieur(e)? (logiciel|etude|d.etude|informatique|r&d|systeme|reseau|"
        r"cloud|devops|data|test|qa|securite|cyber|embarque|full ?stack|back|front|web)",
        r"software (engineer|developer|architect)", r"full ?stack", r"back ?end",
        r"front ?end", r"\bqa\b", r"quality assurance", r"test automation",
        r"\bsre\b", r"\bdevops\b", r"platform engineer", r"cloud engineer",
        r"site reliability", r"infrastructure engineer", r"\bdevsecops\b",
        r"cyber ?securit", r"cybersecurit", r"security engineer", r"pentest",
        r"\brssi\b", r"analyste (soc|cyber|securite)", r"ingenieur securite",
        r"embedded", r"embarque", r"firmware", r"\bfpga\b", r"\bvlsi\b", r"\basic\b",
        r"electronique", r"\biot\b", r"robotique", r"\brobotics\b",
        r"tech ?lead", r"lead tech", r"lead (dev|developer|engineer|technique)",
        r"staff engineer", r"principal engineer",
        r"\bcto\b", r"architecte (logiciel|technique|si|cloud|solution|systeme|data)",
        r"scrum master", r"agile coach", r"\bsysadmin\b", r"administrateur (systeme|"
        r"reseau|base de donnees|infrastructure|cloud|linux|windows)",
        r"\bdba\b", r"integration engineer", r"\bapi\b engineer", r"blockchain",
        r"\bweb3\b", r"game (developer|engineer|programmer)", r"gameplay",
        r"mobile (developer|engineer)", r"\bios\b (developer|engineer)",
        r"\bandroid\b (developer|engineer)", r"\bqa engineer\b",
        r"ingenieur developpement", r"ingenieur validation", r"ingenieur integration",
        r"\bhardware\b", r"hardware engineer", r"\basic\b", r"\brtl\b design",
        # explicit language / stack tokens — a title can be just "Lead Python / Django"
        r"\bpython\b", r"\bjava\b", r"\brust\b", r"\bgolang\b", r"\bkotlin\b",
        r"\bscala\b", r"\belixir\b", r"\bc\+\+", r"\bc#", r"\.net\b", r"\bphp\b",
        r"\bruby\b", r"\btypescript\b", r"\breact\b", r"\bangular\b", r"\bvue\.?js\b",
        r"\bnode\.?js\b", r"\bdjango\b", r"\bsymfony\b", r"\blaravel\b",
        r"spring boot", r"ruby on rails", r"\bkubernetes\b", r"\bterraform\b",
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
        r"consultant (si|erp|sap|salesforce|crm|bi|data|cloud|cyber|it|technique|"
        r"fonctionnel|digital|dynamics|informatique)",
        r"business analyst", r"\bmoa\b", r"\bmoe\b", r"assistance maitrise",
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


def classify(title, profession=None, sector=None, stack=None):
    t = _norm(title)
    prof = _norm(profession)
    blob = t
    if prof:
        blob = blob + " || " + prof
    stack_txt = _norm(stack if not isinstance(stack, str) else [stack])
    stack_hits = len(set(_STACK_HINT.findall(stack_txt))) if stack_txt else 0
    if stack_txt:
        blob = blob + " || tools: " + stack_txt

    # profession taxonomy is authoritative when WTTJ actually set it
    if prof in ("data", "data engineering", "data science", "data analysis",
                "machine learning", "business intelligence"):
        return "data"
    if "product management" in prof or prof in ("product management", "product"):
        return "product"
    if prof in ("ui design", "ux design", "ux/ui design", "product design"):
        return "design"

    # recruiter / sourcer roles are people-ops, never eng/data even when the
    # copy is stuffed with "AI" / "engineering"
    if re.search(r"\b(recruit(er|eur|euse)|sourcer|talent acquisition|"
                 r"charge[e]? de recrutement|talent partner|hr business partner)\b", t):
        return "tech-adjacent" if re.search(
            r"\b(tech|it|engineering|software|dev|data|saas|digital)\b", blob) else None

    if _NEG.search(t) and not re.search(r"logiciel|software|\bdata\b|cyber|devops|"
                                        r"full ?stack|back ?end|front ?end", blob):
        # still allow if an explicit dev/data bucket also fires
        for bucket, pats in _COMPILED:
            if bucket in ("eng", "data") and any(p.search(blob) for p in pats):
                return bucket
        return None

    for bucket, pats in _COMPILED:
        if any(p.search(blob) for p in pats):
            return bucket

    # vague title ("Consultant", "Ingénieur", "Expert F/H") but the WTTJ tools
    # list is unmistakably a dev/data stack -> trust it
    if stack_hits >= 2 and not _NEG.search(t):
        data_tools = re.search(r"\b(spark|airflow|dbt|snowflake|databricks|tensorflow|"
                               r"pytorch|scikit|pandas|power bi|tableau|kafka)\b", stack_txt)
        return "data" if data_tools and not _STACK_HINT.search(t) else "eng"
    return None


CORE = {"eng", "product", "design", "data"}


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
        "Ingénieur systèmes embarqués", "Directeur Commercial",
    ]
    for t in (sys.argv[1:] or tests):
        print("  %-45s -> %s" % (t, classify(t)))
