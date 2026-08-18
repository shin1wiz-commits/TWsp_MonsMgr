#!/usr/bin/env python3
import csv, html, re, time, sys
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE="https://altema.jp"
ZUKAN="https://altema.jp/terrysp/zukan"
OUT=Path("output"); OUT.mkdir(exist_ok=True)
OLD=Path("current_monsters.csv")

# --- Confirmed exception data / reproducibility rules ---
SPECIAL_URLS = {
    80: "https://altema.jp/terrysp/monster/430",
}

CONFIRMED_OVERRIDES = {
    593: {
        "rank": "SS",
        "family": "スライム",
        "reason": "Altema個別ページの基本情報が欠損。ページ上部画像RANK:SSおよびGameWithでSS/スライム系/Gを確認",
        "evidence": "Altema /monster/160 + GameWith No.593",
    },
    594: {
        "rank": "SS",
        "family": "スライム",
        "reason": "Altema個別ページの基本情報が欠損。ページ上部画像RANK:SSおよびGameWithでSS/スライム系/Gを確認",
        "evidence": "Altema /monster/161 + GameWith No.594",
    },
}

S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (Android 13) TerrySP personal data verification/1.0"})

def get(url, tries=3):
    err=None
    for n in range(tries):
        try:
            r=S.get(url,timeout=25)
            r.raise_for_status()
            return r.text
        except Exception as e:
            err=e; time.sleep(1.0*(n+1))
    raise err

def txt(x):
    return re.sub(r"\s+"," ",x.get_text(" ",strip=True)).strip() if x else ""

def normalize_rank(v):
    """Altema表記（Aランク等）をアプリ用（A等）へ正規化する。"""
    v = re.sub(r"\\s+", "", (v or ""))
    m = re.fullmatch(r"(SS|S|A|B|C|D|E|F)ランク", v, re.I)
    if m:
        return m.group(1).upper()
    m = re.fullmatch(r"(SS|S|A|B|C|D|E|F)", v, re.I)
    return m.group(1).upper() if m else v

def normalize_family(v):
    """Altema表記（まじゅう系等）をアプリの系統表記へ正規化する。"""
    v = re.sub(r"\\s+", "", (v or ""))
    if v.endswith("系"):
        v = v[:-1]
    mp={
        "スライム":"スライム",
        "ドラゴン":"ドラゴン",
        "しぜん":"自然",
        "自然":"自然",
        "まじゅう":"魔獣",
        "魔獣":"魔獣",
        "ぶっしつ":"物質",
        "物質":"物質",
        "あくま":"悪魔",
        "悪魔":"悪魔",
        "ゾンビ":"ゾンビ",
        "？？？":"？？？",
        "???":"？？？",
    }
    return mp.get(v,v)

def normalize_size(v):
    """Altema表記（Sサイズ/Mサイズ/Gサイズ）をアプリ用（S/M/G）へ正規化する。"""
    v = re.sub(r"\\s+", "", (v or ""))
    m = re.fullmatch(r"([SMG])サイズ", v, re.I)
    if m:
        return m.group(1).upper()
    m = re.fullmatch(r"([SMG])", v, re.I)
    return m.group(1).upper() if m else v

def zukan_links():
    soup=BeautifulSoup(get(ZUKAN),"html.parser")
    links=[]
    for a in soup.find_all("a",href=True):
        href=a["href"]
        name=txt(a)
        if "/terrysp/monster/" not in href or not name:
            continue
        links.append((name,urljoin(BASE,href)))
    # zukan is ordered by No.; remove duplicate URL/name pairs while preserving order.
    seen=set(); out=[]
    for p in links:
        if p in seen: continue
        seen.add(p); out.append(p)
    return out

def heading_section(soup, heading_text):
    for h in soup.find_all(["h2","h3"]):
        if txt(h)==heading_text:
            level=int(h.name[1]); nodes=[]
            for sib in h.next_siblings:
                if getattr(sib,"name",None) in ("h2","h3") and int(sib.name[1])<=level:
                    break
                nodes.append(str(sib))
            return BeautifulSoup("".join(nodes),"html.parser")
    return None

def first_value_by_label(soup,label):
    for tr in soup.find_all("tr"):
        cells=[txt(c) for c in tr.find_all(["th","td"])]
        for i,v in enumerate(cells[:-1]):
            if v==label:
                return cells[i+1]
    return ""

def parse_page(url, expected_no, expected_name):
    soup=BeautifulSoup(get(url),"html.parser")
    page_text=txt(soup)

    # Basic fields: read the labeled "基本情報" table.
    # Altema currently labels monster size as "枠" (value: Sサイズ/Mサイズ/Gサイズ),
    # while some pages/older layouts may use "サイズ". Support both.
    raw_rank=first_value_by_label(soup,"ランク")
    raw_family=first_value_by_label(soup,"系統")
    raw_size=first_value_by_label(soup,"枠") or first_value_by_label(soup,"サイズ")

    rank=normalize_rank(raw_rank)
    family=normalize_family(raw_family)
    size=normalize_size(raw_size)

    zno=first_value_by_label(soup,"図鑑No.")
    if not zno: zno=first_value_by_label(soup,"図鑑No")
    m=re.search(r"(\d+)",zno)
    page_no=int(m.group(1)) if m else None

    habitat=first_value_by_label(soup,"出現場所")
    weapons=first_value_by_label(soup,"装備可能武器")

    skill=""
    sec=heading_section(soup,"固有スキル")
    if sec:
        for tr in sec.find_all("tr"):
            cells=[txt(c) for c in tr.find_all(["th","td"])]
            if cells and cells[0] not in ("スキル","名称") and "主な取得" not in cells[0]:
                skill=cells[0]; break

    traits=[]
    sec=heading_section(soup,"特性")
    if sec:
        for tr in sec.find_all("tr"):
            cells=[txt(c) for c in tr.find_all(["th","td"])]
            if len(cells)>=2:
                n=re.sub(r"\s*\(\+?\d+\)\s*","",cells[0]).replace("~","〜").replace("～","〜").strip()
                if n and n not in ("名称","特性") and len(n)<=30 and n not in traits:
                    traits.append(n)
    traits=traits[:7]

    # Validation: name must appear in page title/body. No mismatch is silently adopted.
    title=txt(soup.title)
    name_ok=(expected_name in title) or (expected_name in page_text[:3000])
    no_ok=(page_no is None or page_no==expected_no)

    status="OK" if name_ok and no_ok else "要確認"
    notes=[]
    if not name_ok: notes.append("名前照合NG")
    if not no_ok: notes.append(f"図鑑No照合NG(page={page_no})")
    if not rank: notes.append("rank空欄")
    if not family: notes.append("family空欄")
    if not size: notes.append("size空欄（枠/サイズの両ラベルで取得不可）")
    if rank and rank not in {"SS","S","A","B","C","D","E","F"}:
        notes.append(f"rank未正規化(raw={raw_rank})")
    if size and size not in {"S","M","G"}:
        notes.append(f"size未正規化(raw={raw_size})")
    return dict(no=expected_no,name=expected_name,rank=rank,family=family,size=size,
                habitat=habitat,skill=skill,traits=traits,weapons=weapons,
                source_url=url,fetch_status=status,notes=" / ".join(notes))

def main():
    old={}
    with OLD.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f): old[int(r["no"])]=r

    # Use current No/name list as expected identity; zukan links are matched by name.
    zlinks=zukan_links()
    byname={}
    for n,u in zlinks: byname.setdefault(n,u)

    master=[]; failures=[]
    for no in range(1,660):
        exp=old[no]; name=exp["name"]; url=byname.get(name) or SPECIAL_URLS.get(no)
        if not url:
            failures.append(dict(no=no,name=name,stage="URL",reason="図鑑ページから個別URLを取得できませんでした"))
            master.append(dict(no=no,name=name,rank="",family="",size="",habitat="",skill="",
                               trait1="",trait2="",trait3="",trait4="",trait5="",trait6="",trait7="",
                               weapons="",source_url="",fetch_status="失敗",notes="個別URLなし",
                               correction_status="none",correction_reason="",correction_evidence=""))
            continue
        try:
            d=parse_page(url,no,name)

            override = CONFIRMED_OVERRIDES.get(no)
            correction_status = "none"
            correction_reason = ""
            correction_evidence = ""
            if override:
                correction_status = "confirmed_override"
                correction_reason = override["reason"]
                correction_evidence = override["evidence"]
                for key in ("rank","family","size"):
                    if key in override and override[key]:
                        d[key] = override[key]
                d["notes"] = " / ".join(x for x in [d["notes"], f"確認済み補正: {correction_reason}"] if x)
                if "名前照合NG" not in d["notes"] and "図鑑No照合NG" not in d["notes"]:
                    d["fetch_status"] = "OK"

            row={k:d[k] for k in ("no","name","rank","family","size","habitat","skill")}
            for i in range(7): row[f"trait{i+1}"]=d["traits"][i] if i<len(d["traits"]) else ""
            row["weapons"]=d["weapons"]; row["source_url"]=d["source_url"]
            row["fetch_status"]=d["fetch_status"]; row["notes"]=d["notes"]
            row["correction_status"]=correction_status
            row["correction_reason"]=correction_reason
            row["correction_evidence"]=correction_evidence
            master.append(row)
            if d["fetch_status"]!="OK":
                failures.append(dict(no=no,name=name,stage="照合",reason=d["notes"] or "要確認"))
        except Exception as e:
            failures.append(dict(no=no,name=name,stage="取得/解析",reason=str(e)[:300]))
            master.append(dict(no=no,name=name,rank="",family="",size="",habitat="",skill="",
                               trait1="",trait2="",trait3="",trait4="",trait5="",trait6="",trait7="",
                               weapons="",source_url=url,fetch_status="失敗",notes=str(e)[:300],
                               correction_status="none",correction_reason="",correction_evidence=""))
        print(f"{no:03d}/659 {name}", flush=True)
        time.sleep(0.12)

    fields=["no","name","rank","family","size","habitat","skill"]+[f"trait{i}" for i in range(1,8)]+[
        "weapons","source_url","fetch_status","notes",
        "correction_status","correction_reason","correction_evidence"
    ]
    with (OUT/"altema_master_659_new.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(master)

    diffs=[]
    for n in master:
        o=old[n["no"]]
        for field in ("name","rank","family","size"):
            ov=(o.get(field) or "").strip(); nv=(n.get(field) or "").strip()
            if nv and ov!=nv:
                diffs.append(dict(no=n["no"],name=n["name"],field=field,old_value=ov,new_value=nv,
                                  source_url=n["source_url"],status=n["fetch_status"]))
    with (OUT/"altema_diff_report_old_vs_new.csv").open("w",encoding="utf-8-sig",newline="") as f:
        fields2=["no","name","field","old_value","new_value","source_url","status"]
        w=csv.DictWriter(f,fieldnames=fields2); w.writeheader(); w.writerows(diffs)

    with (OUT/"altema_fetch_failures.csv").open("w",encoding="utf-8-sig",newline="") as f:
        fields3=["no","name","stage","reason"]
        w=csv.DictWriter(f,fieldnames=fields3); w.writeheader(); w.writerows(failures)

    applied=[
        {
            "no":x["no"], "name":x["name"],
            "correction_status":x["correction_status"],
            "correction_reason":x["correction_reason"],
            "correction_evidence":x["correction_evidence"],
            "source_url":x["source_url"],
        }
        for x in master if x.get("correction_status")=="confirmed_override"
    ]
    with (OUT/"altema_confirmed_overrides_applied.csv").open("w",encoding="utf-8-sig",newline="") as f:
        fields4=["no","name","correction_status","correction_reason","correction_evidence","source_url"]
        w=csv.DictWriter(f,fieldnames=fields4); w.writeheader(); w.writerows(applied)

    ok=sum(1 for x in master if x["fetch_status"]=="OK")
    print(f"DONE OK={ok} REVIEW/FAIL={659-ok} DIFFS={len(diffs)} OVERRIDES={len(applied)}")
if __name__=="__main__":
    main()
