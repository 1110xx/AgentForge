import yaml, sys

cr = yaml.safe_load(open("deploy/observability/prometheus-rules.yaml", encoding="utf-8"))
cl = yaml.safe_load(open("deploy/observability/prometheus-rules-classic.yaml", encoding="utf-8"))

cr_alerts = {(g["name"], r["alert"]) for g in cr["spec"]["groups"] for r in g["rules"]}
cl_alerts = {
    (g["name"], r.get("alert"))
    for g in cl["groups"]
    for r in g["rules"]
    if "alert" in r
}
miss = cr_alerts - cl_alerts
extra = cl_alerts - cr_alerts
rec = [
    r.get("record")
    for g in cl["groups"]
    for r in g["rules"]
    if "record" in r
]
print("crd_alerts =", len(cr_alerts), "classic_alerts =", len(cl_alerts))
print("missing_in_classic =", sorted(miss) if miss else "NONE")
print("extra_in_classic =", sorted(extra) if extra else "NONE")
print("recording =", rec)
# rules must have exactly one of alert/record and never both/neither
for g in cl["groups"]:
    for r in g["rules"]:
        has_a, has_r = "alert" in r, "record" in r
        assert has_a != has_r, ("rule must be alert XOR record", g["name"], r)
print("schema: alert/record XOR per rule OK")
sys.exit(0 if (not miss and not extra and len(rec) == 2) else 1)