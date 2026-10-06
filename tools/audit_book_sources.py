import csv, json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin, quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

TITLE = "非正常美食文"
POST_IDS = [1307,1304,1303,1298,1297,1293,1290,1285,1284,1279,1277,1276,1275,1272,1271,1269,1268,1265,1264,1263,1255,1254]

ADULT_TERMS = [
    "成人","色情","肉文","肉肉","情色","色文","黄文","欲望","18+","18+","po18",
    "海棠","桃色","私密","福利","sex","porn","erotic","成人小说","第一版主18"
]

UA = "Mozilla/5.0 (compatible; BookSourceAudit/1.0; +https://github.com/OuO3/mimotion)"

def http_json(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
    with urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode("utf-8-sig", errors="replace"))

def parse_header(value):
    if not isinstance(value, str) or not value.strip():
        return {}
    s = value.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def adult_like(src):
    text = " ".join(str(src.get(k,"")) for k in ("bookSourceName","bookSourceGroup","bookSourceComment","bookSourceUrl")).lower()
    return any(t.lower() in text for t in ADULT_TERMS)

def norm_host(url):
    try:
        p = urlparse(str(url))
        host = (p.hostname or "").lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""

def decode_search_definition(src):
    su = src.get("searchUrl")
    if not isinstance(su, str):
        return None, None, None
    s = su.strip()
    if not s or "@js:" in s or "<js>" in s or s.startswith("javascript:"):
        return None, None, None

    method = "GET"
    body = None
    headers = parse_header(src.get("header",""))

    # Handle 阅读's "url, {json request config}" form.
    base = s
    cfg = None
    if "," in s:
        left, right = s.split(",", 1)
        rr = right.strip()
        if rr.startswith("{") and rr.endswith("}"):
            try:
                cfg = json.loads(rr)
                base = left.strip()
                if isinstance(cfg, dict):
                    method = str(cfg.get("method","GET")).upper()
                    body = cfg.get("body")
                    hh = cfg.get("headers")
                    if isinstance(hh, dict):
                        headers.update({str(k):str(v) for k,v in hh.items()})
            except Exception:
                pass

    if not base.startswith("http"):
        bs = src.get("bookSourceUrl","")
        base = urljoin(str(bs).rstrip("/") + "/", base.lstrip("/"))

    # We need an actual searchable template.
    if "{{key}}" not in base and "{{key}}" not in str(body or ""):
        return None, None, None

    base = base.replace("{{key}}", quote(TITLE))
    base = base.replace("{{page}}", "1")
    base = base.replace("{{page+1}}", "2")
    base = base.replace("{{page-1}}", "1")
    if body is not None:
        body = str(body).replace("{{key}}", quote(TITLE)).replace("{{page}}","1").replace("{{page+1}}","2").replace("{{page-1}}","1")
    return base, method, (body, headers)

def test_source(src):
    name = src.get("bookSourceName","")
    host = norm_host(src.get("bookSourceUrl",""))
    result = {
        "name": name, "host": host, "url": src.get("bookSourceUrl",""),
        "searchable_template": False, "http_ok": False, "title_found": False,
        "status": "unverified", "error": ""
    }
    if not host:
        result["status"] = "bad_url"
        return result
    if adult_like(src):
        result["status"] = "excluded"
        return result

    search_url, method, extra = decode_search_definition(src)
    if not search_url:
        result["status"] = "no_testable_search_url"
        return result
    result["searchable_template"] = True
    body, headers = extra
    headers = {"User-Agent": UA, "Accept":"text/html,application/json,text/plain,*/*", **headers}
    try:
        if method == "POST":
            data = (body or "").encode("utf-8")
            req = Request(search_url, data=data, headers={**headers,"Content-Type":"application/x-www-form-urlencoded"}, method="POST")
        else:
            req = Request(search_url, headers=headers, method="GET")
        with urlopen(req, timeout=8) as r:
            raw = r.read()
            result["http_ok"] = 200 <= getattr(r, "status", 200) < 400
        text = raw.decode("utf-8", errors="ignore")
        # direct exact-title match is intentionally strict
        result["title_found"] = TITLE in text
        if result["title_found"]:
            result["status"] = "verified"
        elif result["http_ok"]:
            result["status"] = "reachable_no_title"
        else:
            result["status"] = "http_failed"
    except Exception as e:
        result["error"] = str(e)[:240]
        result["status"] = "request_failed"
    return result

def main():
    sources = []
    fetch_rows = []
    for pid in POST_IDS:
        url = f"https://www.yckceo.com/yuedu/shuyuans/json/id/{pid}.json"
        try:
            data = http_json(url, timeout=30)
            if isinstance(data, list):
                fetch_rows.append((pid, len(data), "ok"))
                sources.extend((pid, x) for x in data if isinstance(x, dict))
            else:
                fetch_rows.append((pid, 0, "not_list"))
        except Exception as e:
            fetch_rows.append((pid, 0, "error:"+str(e)[:160]))

    # Remove exact duplicate source objects first.
    exact = {}
    for pid, src in sources:
        key = json.dumps(src, ensure_ascii=False, sort_keys=True, separators=(",",":"))
        exact.setdefault(key, (pid, src))
    dedup_exact = list(exact.values())

    # One representative per normalized host. Prefer enabled + has searchable URL + latest update.
    by_host = {}
    def score(item):
        pid, s = item
        enabled = 1 if s.get("enabled", True) else 0
        has_search = 1 if isinstance(s.get("searchUrl"), str) and "{{key}}" in s.get("searchUrl","") else 0
        upd = s.get("lastUpdateTime", 0)
        return (0 if adult_like(s) else 1, enabled, has_search, int(upd or 0), pid)

    for item in dedup_exact:
        h = norm_host(item[1].get("bookSourceUrl",""))
        if not h or adult_like(item[1]):
            continue
        if h not in by_host or score(item) > score(by_host[h]):
            by_host[h] = item

    reps = list(by_host.values())

    # Validate in bounded parallelism to avoid hammering sites.
    audit = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(test_source, s): (pid,s) for pid,s in reps}
        for fut in as_completed(futs):
            pid,s = futs[fut]
            try:
                a = fut.result()
            except Exception as e:
                a = {"name":s.get("bookSourceName",""),"host":norm_host(s.get("bookSourceUrl","")),"url":s.get("bookSourceUrl",""),"status":"worker_error","error":str(e)}
            a["post_id"] = pid
            audit.append(a)

    verified_hosts = {a["host"] for a in audit if a.get("status") == "verified"}
    final_items = []
    final_seen = set()
    for pid, s in reps:
        h = norm_host(s.get("bookSourceUrl",""))
        if h in verified_hosts and h not in final_seen:
            final_seen.add(h)
            final_items.append(s)

    # Sort for stable output.
    final_items.sort(key=lambda s: (norm_host(s.get("bookSourceUrl","")), str(s.get("bookSourceName",""))))

    with open("verified_non_normal_food_sources.json","w",encoding="utf-8") as f:
        json.dump(final_items, f, ensure_ascii=False, indent=2)
        f.write("\n")

    with open("audit_non_normal_food_sources.csv","w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f, fieldnames=["post_id","name","host","url","status","searchable_template","http_ok","title_found","error"])
        w.writeheader()
        for a in sorted(audit, key=lambda x:(x.get("status",""),x.get("host",""))):
            w.writerow({k:a.get(k,"") for k in w.fieldnames})

    summary = {
        "title": TITLE,
        "posts_requested": POST_IDS,
        "post_fetch": fetch_rows,
        "raw_source_count": len(sources),
        "exact_dedup_count": len(dedup_exact),
        "safe_host_count": len(reps),
        "verified_count": len(final_items),
        "excluded_count": sum(1 for pid,s in dedup_exact if adult_like(s)),
        "note": "One representative source per normalized host. Only non-adult general-fiction sources are retained."
    }
    with open("audit_summary.json","w",encoding="utf-8") as f:
        json.dump(summary,f,ensure_ascii=False,indent=2)

    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
