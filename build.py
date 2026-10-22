#!/usr/bin/env python3
"""
Google Sheet -> সাইটের ডেটা ফাইল (data/ ফোল্ডার)।

লাখ লাখ পণ্য হলেও সাইট যেন দ্রুত থাকে, তাই এক বিশাল ফাইলের বদলে
ব্র্যান্ড ধরে ছোট ছোট ফাইল বানানো হয়। গ্রাহক যেটা দেখে শুধু সেটাই লোড হয়।

Sheet-এর ট্যাব:
  1) প্রথম ট্যাব  : পণ্যের তালিকা (Drive স্ক্রিপ্ট নিজে ভরে)
  2) brands       : ব্র্যান্ডের নাম/দাম/ছাড়
  3) settings     : ঘোষণা (notice) ও এলাকা (area)
"""
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request

SHEET_ID = os.environ.get("SHEET_ID", "").strip()
LOCAL = os.environ.get("LOCAL_TEST_DIR", "").strip()   # শুধু পরীক্ষার জন্য
OUT = "data"
TMP = "data.new"
PART_SIZE = 1000
NEW_COUNT = 60

CAT = {
    "men": "men", "man": "men", "boys": "men", "ছেলেদের": "men", "ছেলে": "men",
    "women": "women", "woman": "women", "মেয়েদের": "women", "মেয়ে": "women",
    "kids": "kids", "kid": "kids", "শিশুদের": "kids", "শিশু": "kids", "বাচ্চা": "kids",
}
BN = "০১২৩৪৫৬৭৮৯"


def norm_cat(c):
    return CAT.get((c or "").strip().lower(), "others")


def to_num(v):
    if v is None:
        return 0.0
    s = str(v)
    for i, d in enumerate(BN):
        s = s.replace(d, str(i))
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def http_get(url):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (amar-bazar-build)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("utf-8-sig")
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"ডাউনলোড ব্যর্থ: {last}")


def read_csv(text):
    """ফেরত দেয়: (হেডার, সারির তালিকা, মোট সারি)। প্রতিটা সারিতে _row = Sheet-এর আসল সারি নম্বর।"""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], [], 0
    headers = [h.strip().lower() for h in rows[0]]
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        d = {h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(headers)}
        d["_row"] = n
        out.append(d)
    return headers, out, len(rows)


def get_tab(name, first=False):
    """Sheet-এর একটা ট্যাব CSV হিসেবে আনে। না পেলে None।"""
    if LOCAL:
        p = os.path.join(LOCAL, ("products" if first else name) + ".csv")
        if not os.path.exists(p):
            return None
        return open(p, encoding="utf-8-sig").read()
    if not SHEET_ID:
        raise SystemExit("SHEET_ID পাওয়া যায়নি (GitHub Secret সেট করুন)")
    base = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    if first:
        return http_get(base + "/export?format=csv")
    try:
        return http_get(base + "/gviz/tq?tqx=out:csv&headers=1&sheet=" + urllib.parse.quote(name))
    except Exception:
        return None


def drive_id(s):
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", s) or re.search(r"[?&]id=([\w-]+)", s)
    return m.group(1) if m and "drive.google" in s else ""


def img_ref(row):
    fid = row.get("file_id", "").strip()
    if fid:
        return fid
    raw = row.get("img", "").strip()
    if not raw:
        return ""
    return drive_id(raw) or raw


def apply_discount(price, disc):
    d = (disc or "").strip()
    if not d or price <= 0:
        return 0
    if d.endswith("%"):
        final = price * (1 - to_num(d[:-1]) / 100.0)
    else:
        final = price - to_num(d)
    final = int(round(final))
    return final if 0 < final < price else 0


def num(x):
    x = float(x)
    return int(x) if x == int(x) else x


def main():
    ptxt = get_tab("products", first=True)
    ph, prows, total_rows = read_csv(ptxt or "")
    if "category" not in ph and "name" not in ph and "file_id" not in ph:
        print("প্রথম ট্যাব ঠিক নেই (হেডারে category / file_id / name থাকতে হবে)। আগের ডেটা রাখা হলো।")
        sys.exit(1)

    btxt = get_tab("brands")
    bh, brows, _ = read_csv(btxt or "")
    if "brand" not in bh:
        brows = []

    # ---- ব্র্যান্ড তালিকা ----
    brands = {}      # key -> dict
    order = []

    def brand_key(cat, name):
        return f"{cat}|{name}"

    for b in brows:
        cat = norm_cat(b.get("category"))
        name = b.get("brand", "").strip()
        if not name:
            continue
        k = brand_key(cat, name)
        if k in brands:
            continue
        brands[k] = {
            "cat": cat, "brand": name,
            "title": b.get("title", "").strip() or name,
            "prefix": b.get("code", "").strip(),
            "price": to_num(b.get("price")),
            "disc": b.get("discount", "").strip(),
            "items": [],
        }
        order.append(k)

    # ---- পণ্য ----
    flat = []   # (row_no, item, key)
    for r in prows:
        idx = r["_row"]
        ref = img_ref(r)
        if not ref:
            continue
        cat = norm_cat(r.get("category"))
        bname = r.get("brand", "").strip() or "সাধারণ"
        k = brand_key(cat, bname)
        if k not in brands:
            brands[k] = {"cat": cat, "brand": bname, "title": bname, "prefix": "",
                         "price": 0.0, "disc": "", "items": []}
            order.append(k)
        b = brands[k]
        price = to_num(r.get("price"))
        item_disc = to_num(r.get("discount_price"))
        own = 1 if price > 0 else 0                    # নিজস্ব দাম: ব্র্যান্ডের দাম বদলালে এটা বদলাবে না
        if price <= 0:
            price = b["price"]
        if item_disc > 0 and price > 0 and item_disc < price:
            final_disc = int(round(item_disc))
            own += 2                                   # নিজস্ব ছাড়ের দাম
        else:
            final_disc = apply_discount(price, b["disc"])
        code = r.get("code", "").strip() or f"R{idx}"
        name = r.get("name", "").strip()
        item = [code, ref, name, num(price), final_disc, own]
        b["items"].append(item)
        flat.append((idx, item, k))

    if not flat:
        print("কোনো পণ্য পাওয়া যায়নি। আগের ডেটা রাখা হলো।")
        sys.exit(1)

    # ---- লেখা ----
    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    os.makedirs(os.path.join(TMP, "b"))

    brand_list = []
    ids = {}
    for k in order:
        b = brands[k]
        if not b["items"]:
            continue
        bid = hashlib.md5(k.encode("utf-8")).hexdigest()[:8]
        ids[k] = bid
        items = b["items"]
        parts = math.ceil(len(items) / PART_SIZE)
        for p in range(parts):
            chunk = items[p * PART_SIZE:(p + 1) * PART_SIZE]
            with open(os.path.join(TMP, "b", f"{bid}-{p}.json"), "w", encoding="utf-8") as f:
                json.dump(chunk, f, ensure_ascii=False, separators=(",", ":"))
        prices = [(it[4] or it[3]) for it in items if (it[4] or it[3]) > 0]
        brand_list.append({
            "id": bid, "c": b["cat"], "b": b["brand"], "t": b["title"], "x": b["prefix"],
            "n": len(items), "parts": parts, "min": num(min(prices)) if prices else 0,
            "cover": items[-1][1],
        })

    # built = এই বিল্ডে Sheet-এর শেষ সারি; এর পরের সারিগুলো সাইট "লাইভ" ব্যবস্থা থেকে নেয়
    with open(os.path.join(TMP, "brands.json"), "w", encoding="utf-8") as f:
        json.dump({"built": total_rows, "brands": brand_list}, f, ensure_ascii=False, separators=(",", ":"))

    newest = []
    for _, item, k in reversed(flat[-NEW_COUNT:]):
        b = brands[k]
        newest.append([item[0], item[1], item[2] or b["title"], item[3], item[4], item[5], ids[k]])
    with open(os.path.join(TMP, "new.json"), "w", encoding="utf-8") as f:
        json.dump(newest, f, ensure_ascii=False, separators=(",", ":"))

    # ---- settings ----
    settings = None
    stxt = get_tab("settings")
    if stxt:
        sh, srows, _ = read_csv(stxt)
        if "type" in sh and "value" in sh:
            settings = {"notices": [], "areas": []}
            for r in srows:
                t, v = r.get("type", "").lower(), r.get("value", "")
                if not v:
                    continue
                if t == "notice":
                    settings["notices"].append(v)
                elif t == "area":
                    settings["areas"].append(v)
    if settings is None:
        old = os.path.join(OUT, "settings.json")
        if os.path.exists(old):
            shutil.copy(old, os.path.join(TMP, "settings.json"))
        else:
            settings = {"notices": [], "areas": []}
    if settings is not None:
        with open(os.path.join(TMP, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, separators=(",", ":"))

    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.rename(TMP, OUT)
    print(f"ঠিক আছে: {len(flat)} টি পণ্য, {len(brand_list)} টি ব্র্যান্ড")


if __name__ == "__main__":
    main()
