#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TerrySP / Altema 個別モンスターページ 配合再検証ツール v18

目的
----
659体のアルテマ個別ページを巡回し、
「○○の配合表・作り方」に掲載されている「そのモンスターを作る配合」を取得します。

現行アプリの breeding.csv と比較し、
- 一致
- 個別ページで新規発見
- 現行CSVにのみ存在
- 要確認（解析不能 / 特殊表記）
をレポートします。

重要
----
このツールは「自動で現行 breeding.csv を置換」しません。
まず差分を見るための検証専用です。

想定配置
----------
TWsp_MonsMgr/
├─ app/
│  └─ TerrySPMonsterManager_....zip
└─ tools/
   └─ altema-master/
      ├─ current_monsters.csv
      └─ rebuild_altema_breeding.py  ← このファイル

実行
----
cd tools/altema-master
python rebuild_altema_breeding.py

出力
----
output_breeding/
├─ altema_individual_breeding_659.csv
├─ altema_breeding_diff_vs_current.csv
├─ altema_breeding_fetch_failures.csv
├─ altema_breeding_review_needed.csv
├─ altema_breeding_review_summary.csv
├─ altema_breeding_v4_child_validation.csv
├─ altema_breeding_v5_four_body_validation.csv
├─ altema_breeding_v7_four_body_order_and_composition.csv
├─ altema_breeding_v7_four_body_order_conflicts.csv
├─ altema_breeding_v8_source_context_summary.csv
├─ altema_breeding_v8_context_validation.csv
├─ altema_breeding_v9_focus_validation.csv
├─ altema_breeding_v10_boundary_validation.csv
├─ altema_breeding_v11_family_parent_validation.csv
├─ altema_breeding_v12_unresolved_classification.csv
├─ altema_breeding_v12_multi_recipe_validation.csv
├─ altema_breeding_v13_multi_direct_focus.csv
├─ altema_breeding_v15_provenance_audit.csv
├─ altema_breeding_v15_fallback_only.csv
├─ altema_breeding_v15_source_method_summary.csv
├─ altema_breeding_v16_direct_two_cell_focus.csv
├─ altema_breeding_v17_dom_probe.csv
├─ altema_breeding_v17_direct_candidate_probe.csv
├─ altema_breeding_v18_tableline_audit.csv
├─ altema_breeding_v18_focus_validation.csv
├─ altema_breeding_summary.csv
└─ altema_breeding_raw_sections.csv
"""

from __future__ import annotations
from collections import defaultdict

import csv
import io
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


BASE = "https://altema.jp"
ZUKAN = "https://altema.jp/terrysp/zukan"

HERE = Path(__file__).resolve().parent
OUT = HERE / "output_breeding"
MASTER = HERE / "current_monsters.csv"

# No.80 は図鑑一覧からリンク取得に失敗した実績があるため明示フォールバック。
SPECIAL_URLS = {
    80: "https://altema.jp/terrysp/monster/430",
}

# 表記差の許容。
NAME_ALIASES = {
    80: ["ミストウィング", "ミストウイング"],
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Android 13) TerrySP personal breeding verification/1.0"
})

REQUEST_INTERVAL = 0.18


# -----------------------------
# 基本ユーティリティ
# -----------------------------

def clean_text(v: str) -> str:
    v = (v or "").replace("\u3000", " ")
    v = re.sub(r"\s+", " ", v)
    return v.strip()


def node_text(x) -> str:
    if x is None:
        return ""
    return clean_text(x.get_text(" ", strip=True))


def get(url: str, tries: int = 4) -> str:
    last = None
    for n in range(tries):
        try:
            r = SESSION.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or r.encoding
            return r.text
        except Exception as e:
            last = e
            if n + 1 < tries:
                time.sleep(1.2 * (n + 1))
    raise last


def read_master() -> dict[int, dict]:
    if not MASTER.exists():
        raise FileNotFoundError(
            f"{MASTER} がありません。current_monsters.csv と同じフォルダに置いてください。"
        )
    out = {}
    with MASTER.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            out[int(r["no"])] = r
    missing = [n for n in range(1, 660) if n not in out]
    if missing:
        raise RuntimeError(f"current_monsters.csv に欠番があります: {missing[:20]}")
    return out


def find_repo_root() -> Path | None:
    p = HERE
    for _ in range(5):
        if (p / "app").is_dir():
            return p
        p = p.parent
    return None


def newest_app_zip() -> Path | None:
    root = find_repo_root()
    if root is None:
        return None
    zips = sorted(
        (root / "app").glob("*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return zips[0] if zips else None


def read_current_breeding_from_app_zip() -> list[dict]:
    zpath = newest_app_zip()
    if zpath is None:
        print("WARNING: app/*.zip が見つからないため、現行 breeding.csv との比較は省略します。")
        return []

    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        candidates = [
            n for n in names
            if n.endswith("app/src/main/assets/breeding.csv")
            or n.endswith("/assets/breeding.csv")
            or n == "app/src/main/assets/breeding.csv"
        ]
        if not candidates:
            print(f"WARNING: {zpath.name} 内に breeding.csv がありません。比較を省略します。")
            return []
        raw = z.read(candidates[0]).decode("utf-8-sig")

    print(f"現行 breeding.csv: {zpath.name} -> {candidates[0]}")
    return list(csv.DictReader(io.StringIO(raw)))


# -----------------------------
# URL取得
# -----------------------------

def zukan_links() -> list[tuple[str, str]]:
    soup = BeautifulSoup(get(ZUKAN), "html.parser")
    found = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/terrysp/monster/" not in href:
            continue
        name = node_text(a)
        if not name:
            continue
        url = urljoin(BASE, href)
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)

    return found


def build_url_map(master: dict[int, dict]) -> tuple[dict[int, str], list[dict]]:
    links = zukan_links()

    by_name: dict[str, list[str]] = {}
    for name, url in links:
        by_name.setdefault(name, []).append(url)

    result = {}
    failures = []

    for no in range(1, 660):
        if no in SPECIAL_URLS:
            result[no] = SPECIAL_URLS[no]
            continue

        expected = master[no]["name"]
        accepted = NAME_ALIASES.get(no, [expected])

        candidates = []
        for n in accepted:
            candidates.extend(by_name.get(n, []))

        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            result[no] = candidates[0]
        elif len(candidates) > 1:
            # 同名複数URLなら一旦先頭を使うが要確認ログを残す。
            result[no] = candidates[0]
            failures.append({
                "no": no,
                "name": expected,
                "stage": "URL",
                "reason": f"同名URLが複数: {' | '.join(candidates)}",
                "source_url": candidates[0],
            })
        else:
            failures.append({
                "no": no,
                "name": expected,
                "stage": "URL",
                "reason": "図鑑ページから個別URLを取得できませんでした",
                "source_url": "",
            })

    return result, failures


# -----------------------------
# 個別ページの配合セクション抽出
# -----------------------------

def heading_level(tag: Tag) -> int:
    try:
        return int(tag.name[1])
    except Exception:
        return 99


def find_creation_heading(soup: BeautifulSoup, monster_name: str) -> Tag | None:
    """
    「○○の配合表・作り方」に相当する heading を探す。
    個別ページによって若干表記差があっても拾えるようにする。
    """
    best = None
    for h in soup.find_all(["h2", "h3", "h4"]):
        t = node_text(h)
        if not t:
            continue
        if "配合表" in t and ("作り方" in t or monster_name in t):
            if "使う主な配合" in t:
                continue
            best = h
            if "作り方" in t:
                return h
    return best


def section_nodes_after_heading(heading: Tag) -> list[Tag]:
    level = heading_level(heading)
    nodes = []
    for sib in heading.next_siblings:
        if isinstance(sib, Tag) and sib.name in ("h2", "h3", "h4"):
            if heading_level(sib) <= level:
                break
        if isinstance(sib, Tag):
            nodes.append(sib)
    return nodes


def section_soup(nodes: Iterable[Tag]) -> BeautifulSoup:
    return BeautifulSoup("".join(str(x) for x in nodes), "html.parser")


def page_identity(soup: BeautifulSoup) -> tuple[int | None, str]:
    title = node_text(soup.title)
    page_no = None

    for tr in soup.find_all("tr"):
        cells = [node_text(c) for c in tr.find_all(["th", "td"])]
        for i, c in enumerate(cells[:-1]):
            if c in ("図鑑No.", "図鑑No"):
                m = re.search(r"\d+", cells[i + 1])
                if m:
                    page_no = int(m.group())
                    break
        if page_no is not None:
            break

    return page_no, title


def monster_link_name(a: Tag) -> str:
    href = a.get("href", "")
    if "/terrysp/monster/" not in href:
        return ""
    return node_text(a)


def normalize_rule_text(s: str) -> str:
    s = clean_text(s)
    s = s.replace("×", "×")
    s = s.replace("相手を問わず", "相手問わず")
    s = s.replace("相手は問わず", "相手問わず")
    return s


SECTION_MARKERS = ("【主な配合】", "【特殊配合】", "【位階配合】", "【配合】")
NON_RECIPE_MARKERS = ("【入手方法】", "【出現場所】", "【スカウト】", "【タマゴ】", "【卵】")

def strip_after_non_recipe_marker(s: str) -> str:
    """配合本文の途中に入手方法等が混ざる場合、それ以降を配合親として扱わない。"""
    s = normalize_rule_text(s)
    cut = len(s)
    for marker in NON_RECIPE_MARKERS:
        i = s.find(marker)
        if i >= 0:
            cut = min(cut, i)
    return s[:cut].strip()

def recipe_only_text(s: str) -> str:
    """
    v3:
    配合情報と入手方法が同居する文字列から配合部分だけを残す。
    【入手方法】などは明確な終了境界。
    """
    s = normalize_rule_text(s)
    if not s:
        return ""

    # 最初の非配合セクション以降を完全に切る
    positions = [s.find(m) for m in NON_RECIPE_MARKERS if s.find(m) >= 0]
    if positions:
        s = s[:min(positions)]

    # 配合開始マーカーはラベルだけ除去
    for marker in SECTION_MARKERS:
        s = s.replace(marker, "\n")

    return s.strip()


def split_recipe_fragments(s: str) -> list[str]:
    """
    v3:
    配合部分だけを抽出し、改行・句点単位へ分割。
    「×」を持つ断片だけ返す。
    """
    s = recipe_only_text(s)
    if not s:
        return []

    parts = re.split(r"[\n。；;]+", s)
    out = []
    for p in parts:
        p = clean_text(p)
        if "×" in p:
            out.append(p)
    return out


def clean_parent_phrase(s: str) -> str:
    s = clean_text(s)
    s = re.sub(r"^[・：:,\s]+|[・：:,\s]+$", "", s)
    return s


def expand_or_side(s: str, known_names: set[str]) -> list[str]:
    """
    B or C / BまたはC / B・C 等の親候補を安全に展開。
    中黒は既知モンスター名へ分解できる場合だけ使用する。
    """
    s = clean_parent_phrase(s)
    if not s:
        return []

    # 括弧は外側だけ除去
    if (s.startswith("(") and s.endswith(")")) or (s.startswith("（") and s.endswith("）")):
        s = clean_parent_phrase(s[1:-1])

    # 明示的OR
    parts = re.split(r"\s+(?:or|OR)\s+|または|もしくは|あるいは", s)
    parts = [clean_parent_phrase(x) for x in parts if clean_parent_phrase(x)]
    if len(parts) > 1:
        return parts

    # "/" は複数候補表現として使われることがある
    slash = [clean_parent_phrase(x) for x in re.split(r"[／/]", s) if clean_parent_phrase(x)]
    if len(slash) > 1 and all(x in known_names for x in slash):
        return slash

    # 中黒はモンスター名自体にも入り得るため、全片が既知名のときだけ分解
    dot = [clean_parent_phrase(x) for x in s.split("・") if clean_parent_phrase(x)]
    if len(dot) > 1 and all(x in known_names for x in dot):
        return dot

    return [s]


def extract_pairs_from_fragment(frag: str, known_names: set[str]) -> list[tuple[str, str, str]]:
    """
    A × B、A × (B or C) を独立した親ペアへ展開。
    返り値: (left, right, note)
    """
    frag = recipe_only_text(frag)
    if "×" not in frag:
        return []

    # まず単純な1個の×を優先。
    if frag.count("×") == 1:
        left, right = [clean_parent_phrase(x) for x in frag.split("×", 1)]
        lefts = expand_or_side(left, known_names)
        rights = expand_or_side(right, known_names)
        return [(l, r, "OR展開" if len(lefts) > 1 or len(rights) > 1 else "")
                for l in lefts for r in rights if l and r]

    # 複数×: 既知モンスター名を使って A×B C×D 型を抽出
    names = sorted(known_names, key=len, reverse=True)
    name_alt = "|".join(re.escape(n) for n in names)
    rule_alt = r"(?:相手問わず|(?:スライム|ドラゴン|魔獣|自然|物質|悪魔|ゾンビ|？？？)系(?:モンスター)?)"
    atom = rf"(?:{name_alt}|{rule_alt})"
    pat = re.compile(rf"({atom})\s*×\s*({atom})")
    pairs = [(clean_parent_phrase(m.group(1)), clean_parent_phrase(m.group(2)), "複数配合分割")
             for m in pat.finditer(frag)]
    return pairs


def family_of_no(master: dict[int, dict], no: int) -> str:
    return clean_text(master.get(no, {}).get("family", ""))

def rule_matches_monster(rule: str, monster_no: int, master: dict[int, dict]) -> bool:
    """
    配合ルールが具体モンスターを包含するか。
    現時点では安全に判定できるものだけ true。
    """
    rule = normalize_rule_text(rule)
    if not rule:
        return False
    if rule == "相手問わず":
        return True

    fam = family_of_no(master, monster_no)
    fam_rule_map = {
        "スライム系": "スライム",
        "ドラゴン系": "ドラゴン",
        "魔獣系": "魔獣",
        "自然系": "自然",
        "物質系": "物質",
        "悪魔系": "悪魔",
        "ゾンビ系": "ゾンビ",
        "？？？系": "？？？",
    }
    if rule in fam_rule_map:
        return fam == fam_rule_map[rule]

    # ランク/サイズなど未実装の複合ルールは安全のため包含扱いしない
    return False

def recipe_parent_atoms(r: dict) -> list[tuple[str, str, str]]:
    atoms = []
    for i in range(1, 5):
        t = clean_text(r.get(f"parent{i}_type", ""))
        no = clean_text(r.get(f"parent{i}_no", ""))
        name = normalize_rule_text(r.get(f"parent{i}", ""))
        if t or no or name:
            atoms.append((t, no, name))
    return atoms

def pair_rule_covers_specific(general: dict, specific: dict, master: dict[int, dict]) -> bool:
    """
    同じ子について、general が specific を包含するかを判定。
    親順不同。2体配合のみを安全に対象にする。
    """
    ga = recipe_parent_atoms(general)
    sa = recipe_parent_atoms(specific)
    if len(ga) != 2 or len(sa) != 2:
        return False

    def atom_covers(g, s):
        gt, gno, gname = g
        st, sno, sname = s

        if gt == "monster" and st == "monster":
            return bool(gno and sno and int(gno) == int(sno))

        if gt in ("rule", "family") and st == "monster" and sno:
            return rule_matches_monster(gname, int(sno), master)

        if gt in ("rule", "family") and st in ("rule", "family"):
            if gt == "family" and st == "family":
                return normalize_family_rule_text(gname) == normalize_family_rule_text(sname)
            return gname == sname or gname == "相手問わず"

        return False

    return (
        atom_covers(ga[0], sa[0]) and atom_covers(ga[1], sa[1])
    ) or (
        atom_covers(ga[0], sa[1]) and atom_covers(ga[1], sa[0])
    )




FAMILY_RULES = (
    "スライム系",
    "ドラゴン系",
    "魔獣系",
    "自然系",
    "物質系",
    "悪魔系",
    "ゾンビ系",
    "？？？系",
)

def normalize_family_rule_text(s: str) -> str:
    """
    悪魔系モンスター / 悪魔系 を同じ「悪魔系」へ正規化。
    """
    s = normalize_rule_text(s)
    s = s.replace("モンスター", "")
    s = clean_text(s)
    for fam in FAMILY_RULES:
        if fam in s:
            return fam
    return ""

def family_rule_in_text(s: str) -> str:
    """
    文字列中から系統指定を1つ取得。
    """
    s = normalize_rule_text(s)
    for fam in FAMILY_RULES:
        if fam in s:
            return fam
    return ""

def extract_direct_monster_family_pair(
    seg: str,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
) -> list[RecipeCandidate]:
    """
    v11:
    直接配合領域の
      固定モンスター × ○○系
    を HIGH で正式取得する。
    """
    seg = cut_at_semantic_boundary(seg)
    fam = family_rule_in_text(seg)
    if not fam:
        return []

    names = known_monsters_in_text_order(seg, name_to_no, exclude={result_name})
    if not names:
        return []

    fixed = names[0]
    p1 = make_parent_token(fixed, name_to_no)

    return [RecipeCandidate(
        result_no=result_no,
        result_name=result_name,
        parent1_type=p1[0],
        parent1_no=p1[1],
        parent1=p1[2],
        parent2_type="family",
        parent2_no="",
        parent2=fam,
        recipe_type="2体配合",
        confidence="HIGH",
        note="v11 直接配合・系統指定親",
        source_url=source_url,
        source_context=SOURCE_DIRECT,
        raw_text=seg,
    )]


def classify_parent_token(
    text: str,
    link_names: list[str],
    name_to_no: dict[str, int],
) -> tuple[str, str, str]:
    """
    return: (parent_type, parent_no, parent_name_or_rule)
    """
    text = normalize_rule_text(text)

    # monsterリンクが1つだけならモンスターとして優先。
    unique_links = [x for x in dict.fromkeys(link_names) if x]
    known_links = [x for x in unique_links if x in name_to_no]
    if len(known_links) == 1:
        n = known_links[0]
        return "monster", str(name_to_no[n]), n

    # 相手問わず
    if "相手問わず" in text:
        return "rule", "", "相手問わず"

    # 系統ルール
    family_patterns = [
        "スライム系", "ドラゴン系", "魔獣系", "自然系",
        "物質系", "悪魔系", "ゾンビ系", "？？？系"
    ]
    for p in family_patterns:
        if p in text:
            return "family", "", p

    # 「○○系モンスター」表記
    m = re.search(r"([^\s×|]+系)モンスター", text)
    if m:
        return "family", "", m.group(1)

    # monsterリンクが複数ある時は曖昧。
    if known_links:
        return "review", "", " / ".join(known_links)

    if text:
        return "rule", "", text
    return "review", "", ""


@dataclass
class RecipeCandidate:
    result_no: int
    result_name: str
    parent1_type: str
    parent1_no: str
    parent1: str
    parent2_type: str
    parent2_no: str
    parent2: str
    parent3_type: str = ""
    parent3_no: str = ""
    parent3: str = ""
    parent4_type: str = ""
    parent4_no: str = ""
    parent4: str = ""
    recipe_type: str = "2体配合"
    confidence: str = "HIGH"
    note: str = ""
    source_url: str = ""
    source_context: str = "UNKNOWN"
    source_method: str = "UNCLASSIFIED"
    is_fallback: bool = False
    fallback_reason: str = ""
    raw_text: str = ""



UI_LABELS_4BODY = {
    "親A", "親B", "親Aへの配合ルート", "親Bへの配合ルート",
    "親1", "親2", "親3", "親4",
}

def known_monsters_in_cell(cell: Tag, name_to_no: dict[str, int]) -> list[str]:
    """セル内リンクから既知モンスター名を重複なしで取得。"""
    out = []
    for a in cell.find_all("a", href=True):
        n = monster_link_name(a)
        if n and n in name_to_no and n not in out:
            out.append(n)
    # リンクがない場合はセル全文がモンスター名なら補完
    t = clean_text(node_text(cell))
    if t in name_to_no and t not in out:
        out.append(t)
    return out


def make_parent_token(name: str, name_to_no: dict[str, int]) -> tuple[str, str, str]:
    """既知モンスター名を優先して親トークン化。"""
    name = clean_parent_phrase(name)
    if name in name_to_no:
        return "monster", str(name_to_no[name]), name
    return classify_parent_token(name, [name] if name in name_to_no else [], name_to_no)


SOURCE_DIRECT = "DIRECT_PATTERN"
SOURCE_ROUTE = "CREATION_ROUTE"
SOURCE_USED_IN = "USED_IN_BREEDING"
SOURCE_GLOBAL_4BODY = "GLOBAL_4BODY_TABLE"
SOURCE_UNKNOWN = "UNKNOWN"


METHOD_DIRECT_STRUCTURED = "DIRECT_STRUCTURED"
METHOD_ROUTE_STRUCTURED = "ROUTE_STRUCTURED"
METHOD_GLOBAL_4BODY = "GLOBAL_4BODY_STRUCTURED"
METHOD_USED_IN = "USED_IN_STRUCTURED"
METHOD_ROW_STRUCTURED = "ROW_STRUCTURED"
METHOD_TEXT_FALLBACK = "TEXT_FALLBACK"
METHOD_LINK_ANY_FALLBACK = "FALLBACK_LINK_ANY"
METHOD_UNKNOWN = "UNKNOWN"

def apply_provenance_v15(c: RecipeCandidate) -> RecipeCandidate:
    """
    v15:
    既存パーサーの note/source_context から取得経路を確定する。
    明示済み source_method があれば上書きしない。
    """
    if getattr(c, "source_method", "UNCLASSIFIED") not in ("", "UNCLASSIFIED"):
        return c

    note = clean_text(getattr(c, "note", "") or "")
    ctx = getattr(c, "source_context", SOURCE_UNKNOWN)

    if "セクション内リンク＋相手問わずから補完" in note:
        c.source_method = METHOD_LINK_ANY_FALLBACK
        c.is_fallback = True
        c.fallback_reason = "構造化解析で候補を確定できず、セクション内リンクと「相手問わず」を組み合わせて補完"
        return c

    if "テキストフォールバック" in note or "テキスト断片" in note:
        c.source_method = METHOD_TEXT_FALLBACK
        c.is_fallback = True
        c.fallback_reason = "table/構造化解析で確定できず、テキスト断片から補完"
        return c

    if ctx == SOURCE_DIRECT:
        c.source_method = METHOD_DIRECT_STRUCTURED
    elif ctx == SOURCE_ROUTE:
        c.source_method = METHOD_ROUTE_STRUCTURED
    elif ctx == SOURCE_GLOBAL_4BODY:
        c.source_method = METHOD_GLOBAL_4BODY
    elif ctx == SOURCE_USED_IN:
        c.source_method = METHOD_USED_IN
    elif any(x in note for x in (
        "親セル解析", "構造化テキスト抽出", "左セル=子",
        "4列UI", "4体配合", "配合式"
    )):
        c.source_method = METHOD_ROW_STRUCTURED
    else:
        c.source_method = METHOD_UNKNOWN

    c.is_fallback = False
    c.fallback_reason = ""
    return c


def provenance_row_v15(r: dict) -> dict:
    return {
        "result_no": r.get("result_no",""),
        "result_name": r.get("result_name",""),
        "recipe_type": r.get("recipe_type",""),
        "parent1": r.get("parent1",""),
        "parent2": r.get("parent2",""),
        "parent3": r.get("parent3",""),
        "parent4": r.get("parent4",""),
        "confidence": r.get("confidence",""),
        "source_context": r.get("source_context","UNKNOWN"),
        "source_method": r.get("source_method","UNKNOWN"),
        "is_fallback": r.get("is_fallback",False),
        "fallback_reason": r.get("fallback_reason",""),
        "note": r.get("note",""),
        "source_url": r.get("source_url",""),
        "raw_text": r.get("raw_text",""),
    }

def build_candidate(
    *,
    result_no: int,
    result_name: str,
    parents: list[str],
    name_to_no: dict[str, int],
    source_url: str,
    raw_text: str,
    recipe_type: str,
    note: str,
    confidence: str = "HIGH",
    source_context: str = SOURCE_UNKNOWN,
) -> RecipeCandidate | None:
    """2体/4体の共通Candidate生成。"""
    if recipe_type == "2体配合" and len(parents) != 2:
        return None
    if recipe_type == "4体配合" and len(parents) != 4:
        return None

    toks = [make_parent_token(p, name_to_no) for p in parents]
    if any(not t[2] for t in toks):
        return None
    if any(t[0] == "review" for t in toks):
        confidence = "REVIEW"

    while len(toks) < 4:
        toks.append(("", "", ""))

    return RecipeCandidate(
        result_no=result_no,
        result_name=result_name,
        parent1_type=toks[0][0], parent1_no=toks[0][1], parent1=toks[0][2],
        parent2_type=toks[1][0], parent2_no=toks[1][1], parent2=toks[1][2],
        parent3_type=toks[2][0], parent3_no=toks[2][1], parent3=toks[2][2],
        parent4_type=toks[3][0], parent4_no=toks[3][1], parent4=toks[3][2],
        recipe_type=recipe_type,
        confidence=confidence,
        note=note,
        source_url=source_url,
        source_context=source_context,
        raw_text=raw_text,
    )




def expand_four_body_compact_expression(expr: str, name_to_no: dict[str, int]) -> list[str] | None:
    """
    v6:
    4体配合の圧縮表記を4親へ正規化する。

    対応例:
      A×4 -> A,A,A,A
      A×4体 -> A,A,A,A
      (A×B)×2 -> A,B,A,B
      A×2 B×2 -> A,A,B,B
      A B C D -> 4つの既知名ならそのまま
    """
    # v9: 後ろに【入手方法】等が連結されていても4体式だけを使う
    s = recipe_only_text(expr)
    s = normalize_rule_text(s)
    s = s.replace("【4体配合】", " ")
    s = re.sub(r"4体配合(?:の方法はこちら)?", " ", s)
    s = clean_text(s)

    # (A×B)×2
    m = re.fullmatch(r"[（(]\s*(.+?)\s*×\s*(.+?)\s*[）)]\s*×\s*2", s)
    if m:
        a, b = clean_parent_phrase(m.group(1)), clean_parent_phrase(m.group(2))
        if a in name_to_no and b in name_to_no:
            return [a, b, a, b]

    # A×4 / A×4体
    m = re.fullmatch(r"(.+?)\s*×\s*4(?:体)?", s)
    if m:
        a = clean_parent_phrase(m.group(1))
        if a in name_to_no:
            return [a, a, a, a]

    # A×2 B×2 / A×2・B×2
    m = re.fullmatch(r"(.+?)\s*×\s*2\s*[・,、\s]+\s*(.+?)\s*×\s*2", s)
    if m:
        a, b = clean_parent_phrase(m.group(1)), clean_parent_phrase(m.group(2))
        if a in name_to_no and b in name_to_no:
            return [a, a, b, b]

    # 明示 A×B×C×D
    parts = [clean_parent_phrase(x) for x in s.split("×")]
    parts = [p for p in parts if p]
    if len(parts) == 4 and all(p in name_to_no for p in parts):
        return parts

    return None


def extract_four_body_from_ui_table(
    table: Tag,
    page_result_no: int,
    page_result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
    source_context: str = SOURCE_UNKNOWN,
) -> RecipeCandidate | None:
    """
    v6:
    個別ページの4体配合UIから、4つの祖父母枠を直接拾う。
    「親A/親B/親Aへの配合ルート/親Bへの配合ルート」はUIラベルとして無視。
    """
    if table is None:
        return None
    table_text = normalize_rule_text(node_text(table))
    if "4体配合" not in table_text:
        return None

    # 既知モンスター名をDOM順で収集
    names = []
    for a in table.find_all("a", href=True):
        n = monster_link_name(a)
        if n and n in name_to_no and n not in UI_LABELS_4BODY:
            names.append(n)

    # ページ主役自身や重複した上段リンクが紛れうるため、
    # 末尾4件を「4つの配合元枠」として採用する。
    if len(names) < 4:
        return None

    parents = names[-4:]
    if not all(p in name_to_no for p in parents):
        return None

    return build_candidate(
        result_no=page_result_no,
        result_name=page_result_name,
        parents=parents,
        name_to_no=name_to_no,
        source_url=source_url,
        raw_text=table_text,
        recipe_type="4体配合",
        note="v8 4体配合UIの4枠を直接取得",
        confidence="HIGH",
        source_context=source_context,
    )


def four_body_ordered_signature(r: dict) -> tuple:
    """
    v7:
    4体配合の「原順序」を保持した署名。
    例:
      A,B,A,B と A,A,B,B は別物として扱う。
    """
    return (
        int(r.get("result_no") or 0),
        tuple(clean_text(r.get(f"parent{i}", "")) for i in range(1, 5)),
    )


def four_body_pairing_signature(r: dict) -> tuple:
    """
    4体配合の2+2構造を保持した署名。
    各ペア内部の左右だけは順不同として正規化するが、
    (親1,親2) と (親3,親4) の組み分けは保持する。
    """
    p = [clean_text(r.get(f"parent{i}", "")) for i in range(1, 5)]
    pair1 = tuple(sorted((p[0], p[1])))
    pair2 = tuple(sorted((p[2], p[3])))
    return (
        int(r.get("result_no") or 0),
        pair1,
        pair2,
    )


def four_body_composition_signature(r: dict) -> tuple:
    """
    4体配合の「構成だけ」を比較する署名。
    順序・2+2の組み分けを無視し、4体の多重集合として扱う。
    """
    return (
        int(r.get("result_no") or 0),
        tuple(sorted(clean_text(r.get(f"parent{i}", "")) for i in range(1, 5))),
    )


def four_body_composition_text(r: dict) -> str:
    """
    A,A,B,B -> A×2 / B×2 のような読みやすい構成表現。
    """
    parents = [clean_text(r.get(f"parent{i}", "")) for i in range(1, 5)]
    counts = {}
    order = []
    for p in parents:
        if not p:
            continue
        if p not in counts:
            counts[p] = 0
            order.append(p)
        counts[p] += 1
    return " / ".join(f"{p}×{counts[p]}" if counts[p] > 1 else p for p in order)






def nearest_heading_text(node: Tag) -> str:
    """直前の見出し相当要素から、この表の意味を推定する。"""
    cur = node
    for _ in range(50):
        cur = cur.find_previous()
        if cur is None:
            break
        name = getattr(cur, "name", "")
        if name in ("h2", "h3", "h4", "h5", "strong"):
            t = normalize_rule_text(node_text(cur))
            if t:
                return t
        if name == "div":
            t = normalize_rule_text(node_text(cur))
            if 0 < len(t) <= 140 and any(k in t for k in (
                "への配合パターン",
                "までの作成ルート",
                "を使う主な配合",
                "4体配合表",
            )):
                return t
    return ""

def detect_source_context(node: Tag, page_result_name: str) -> tuple[str, str]:
    heading = nearest_heading_text(node)

    if f"{page_result_name}への配合パターン" in heading:
        return SOURCE_DIRECT, heading
    if "までの作成ルート" in heading or "入手方法" in heading:
        return SOURCE_ROUTE, heading
    if "を使う主な配合" in heading:
        return SOURCE_USED_IN, heading
    if "4体配合表" in heading:
        return SOURCE_GLOBAL_4BODY, heading
    return SOURCE_UNKNOWN, heading


def compact_match_text(s: str) -> str:
    """名前照合用。空白・中点・全角空白など表示差を吸収する。"""
    s = normalize_rule_text(s)
    s = re.sub(r"[\s・･]", "", s)
    return s


def known_monsters_in_text_order(
    text_value: str,
    name_to_no: dict[str, int],
    exclude: set[str] | None = None,
) -> list[str]:
    """
    文字列中に現れる既知モンスターを表示順で返す。
    長い名前を優先し、部分一致の二重取得を避ける。
    """
    exclude = exclude or set()
    compact = compact_match_text(text_value)

    candidates = []
    # 同じNoの別名が複数ヒットしても、最終的には正式名へ寄せる
    no_to_canonical = {}
    for name, no in name_to_no.items():
        if no not in no_to_canonical:
            no_to_canonical[no] = name

    variants = sorted(
        ((compact_match_text(name), name, no) for name, no in name_to_no.items()),
        key=lambda x: len(x[0]),
        reverse=True,
    )

    occupied = []
    for key, name, no in variants:
        if not key or name in exclude:
            continue
        start = 0
        while True:
            idx = compact.find(key, start)
            if idx < 0:
                break
            span = (idx, idx + len(key))
            if not any(not (span[1] <= a or span[0] >= b) for a, b in occupied):
                occupied.append(span)
                candidates.append((idx, no_to_canonical.get(no, name), no))
            start = idx + 1

    candidates.sort(key=lambda x: x[0])

    # 同一位置・同一Noの重複別名だけ除外。実際に2回出る同一モンスターは保持する。
    out = []
    seen_pos_no = set()
    for idx, name, no in candidates:
        key = (idx, no)
        if key in seen_pos_no:
            continue
        seen_pos_no.add(key)
        out.append(name)
    return out


def extract_direct_pattern_segment(raw_section: str, page_name: str) -> str:
    """
    「Xへの配合パターン」から、作成ルート/入手方法が始まる直前までを切り出す。
    """
    markers = [
        f"{page_name}への配合パターン",
        f"{page_name}への配合パターン(特殊配合)",
        f"{page_name}への配合パターン（特殊配合）",
    ]
    start = -1
    used = ""
    for marker in markers:
        i = raw_section.find(marker)
        if i >= 0:
            start = i + len(marker)
            used = marker
            break
    if start < 0:
        return ""

    tail = raw_section[start:]
    end_candidates = []
    for stop in (
        f"▼{page_name}までの作成ルートを見る▼",
        f"{page_name}までの作成ルート",
        f"{page_name}の入手方法",
        f"{page_name}を使う主な配合",
        "モンスター 配合・入手方法",
    ):
        j = tail.find(stop)
        if j >= 0:
            end_candidates.append(j)

    if end_candidates:
        tail = tail[:min(end_candidates)]

    tail = cut_at_semantic_boundary(tail)
    return clean_text(tail)



DIRECT_STOP_PATTERNS = (
    "の入手方法",
    "までの作成ルート",
    "を使う主な配合",
    "モンスター 配合・入手方法",
    "モンスター配合・入手方法",
)

def cut_at_semantic_boundary(s: str) -> str:
    """
    v10:
    「○○の入手方法」「○○までの作成ルート」等へ入る直前で切る。
    """
    s = normalize_rule_text(s)
    cut = len(s)

    for pat in DIRECT_STOP_PATTERNS:
        i = s.find(pat)
        if i >= 0:
            cut = min(cut, i)

    for pattern in (
        r"([^\s|]+?)の入手方法",
        r"([^\s|]+?)までの作成ルート",
        r"([^\s|]+?)を使う主な配合",
    ):
        m = re.search(pattern, s)
        if m:
            cut = min(cut, m.start())

    return clean_text(s[:cut])


def parse_any_partner_direct(
    seg: str,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
) -> list[RecipeCandidate]:
    """
    直接配合領域に「相手問わず」がある場合は、
    固定親1体 + 相手問わず を最優先で確定する。
    """
    seg = cut_at_semantic_boundary(seg)
    if "相手問わず" not in seg:
        return []

    names = known_monsters_in_text_order(
        seg, name_to_no, exclude={result_name}
    )
    if not names:
        return []

    p1 = make_parent_token(names[0], name_to_no)
    return [RecipeCandidate(
        result_no=result_no,
        result_name=result_name,
        parent1_type=p1[0],
        parent1_no=p1[1],
        parent1=p1[2],
        parent2_type="rule",
        parent2_no="",
        parent2="相手問わず",
        recipe_type="2体配合",
        confidence="HIGH",
        note="v10 直接配合・相手問わず最優先",
        source_url=source_url,
        source_context=SOURCE_DIRECT,
        raw_text=seg,
    )]


def parse_direct_pattern_from_raw_section(
    raw_section: str,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
) -> list[RecipeCandidate]:
    """
    v9:
    ページ主対象の直接配合だけを raw_section の専用領域から確定する。

    - 親A/親B + 4つのモンスター枠 -> 4体配合
    - 2モンスター -> 2体配合
    - 1モンスター + 相手問わず -> 2体配合
    """
    seg = extract_direct_pattern_segment(raw_section, result_name)
    if not seg:
        return []
    seg = cut_at_semantic_boundary(seg)

    any_partner = parse_any_partner_direct(
        seg, result_no, result_name, name_to_no, source_url
    )
    if any_partner:
        return any_partner

    family_pair = extract_direct_monster_family_pair(
        seg, result_no, result_name, name_to_no, source_url
    )
    if family_pair:
        return family_pair

    names = known_monsters_in_text_order(
        seg, name_to_no, exclude={result_name}
    )

    # 4枠UI: 「親A」「親B」「親Aへの配合ルート」「親Bへの配合ルート」
    # が揃い、4体以上の既知モンスターが並ぶ場合は最初の4枠を直接配合として採用。
    is_four_ui = (
        "親A" in seg
        and "親B" in seg
        and "親Aへの配合ルート" in seg
        and "親Bへの配合ルート" in seg
        and len(names) >= 4
    )
    if is_four_ui:
        parents = names[:4]
        c = build_candidate(
            result_no=result_no,
            result_name=result_name,
            parents=parents,
            name_to_no=name_to_no,
            source_url=source_url,
            raw_text=seg,
            recipe_type="4体配合",
            note="v9 raw_section主対象4枠を直接取得",
            confidence="HIGH",
            source_context=SOURCE_DIRECT,
        )
        return [c] if c else []

    # 明示4体式が直接領域にある場合
    if ("4体配合" in seg or "×4" in seg or "× 4" in seg) and "×" in seg:
        parents = expand_four_body_compact_expression(seg, name_to_no)
        if parents and len(parents) == 4:
            c = build_candidate(
                result_no=result_no,
                result_name=result_name,
                parents=parents,
                name_to_no=name_to_no,
                source_url=source_url,
                raw_text=seg,
                recipe_type="4体配合",
                note="v9 raw_section主対象4体式を正規化",
                confidence="HIGH",
                source_context=SOURCE_DIRECT,
            )
            return [c] if c else []

    # 通常2体
    if len(names) >= 2:
        c = build_candidate(
            result_no=result_no,
            result_name=result_name,
            parents=names[:2],
            name_to_no=name_to_no,
            source_url=source_url,
            raw_text=seg,
            recipe_type="2体配合",
            note="v9 raw_section主対象2体配合を直接取得",
            confidence="HIGH",
            source_context=SOURCE_DIRECT,
        )
        return [c] if c else []

    # 相手問わず
    if len(names) == 1 and "相手問わず" in seg:
        p1 = make_parent_token(names[0], name_to_no)
        p2 = ("any", "", "相手問わず")
        return [RecipeCandidate(
            result_no=result_no,
            result_name=result_name,
            parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
            parent2_type=p2[0], parent2_no=p2[1], parent2=p2[2],
            recipe_type="2体配合",
            confidence="HIGH",
            note="v10 raw_section主対象・相手問わず",
            source_url=source_url,
            source_context=SOURCE_DIRECT,
            raw_text=seg,
        )]

    return []


def same_recipe_ordered(a: RecipeCandidate, b: RecipeCandidate) -> bool:
    return (
        a.result_no == b.result_no
        and a.recipe_type == b.recipe_type
        and [a.parent1, a.parent2, a.parent3, a.parent4]
           == [b.parent1, b.parent2, b.parent3, b.parent4]
    )

def parse_explicit_four_body_row(
    child_name: str,
    expr: str,
    name_to_no: dict[str, int],
    source_url: str,
    raw_text: str,
    source_context: str = SOURCE_UNKNOWN,
) -> RecipeCandidate | None:
    """
    v6:
    明示4体配合を圧縮表記も含めて4親へ正規化。
    """
    if child_name not in name_to_no:
        return None

    raw_expr = normalize_rule_text(expr)
    if "4体配合" not in raw_expr and "×4" not in raw_expr and "× 4" not in raw_expr:
        return None

    parents = expand_four_body_compact_expression(raw_expr, name_to_no)
    if not parents or len(parents) != 4:
        return None

    return build_candidate(
        result_no=name_to_no[child_name],
        result_name=child_name,
        parents=parents,
        name_to_no=name_to_no,
        source_url=source_url,
        raw_text=raw_text,
        recipe_type="4体配合",
        note="v8 明示/圧縮4体配合を正規化",
        confidence="HIGH",
        source_context=source_context,
    )


def parse_child_recipe_expression(
    child_name: str,
    expr: str,
    name_to_no: dict[str, int],
    source_url: str,
    raw_text: str,
    source_context: str = SOURCE_UNKNOWN,
) -> list[RecipeCandidate]:
    """
    v4の本丸:
      左セル = 子
      右セル = 親の式
    として解釈する。

    例:
      ブリザード | フレイム × (イエティ or スノードラゴン)
      -> フレイム×イエティ -> ブリザード
      -> フレイム×スノードラゴン -> ブリザード

      キングモーモン | ピンクモーモン×マポレーナ×ティコ×ククリ
      -> 4体配合1件
    """
    if child_name not in name_to_no:
        return []

    child_no = name_to_no[child_name]
    expr = recipe_only_text(expr)
    if not expr or "×" not in expr:
        return []

    # 明示的な4体配合: A×B×C×D
    pieces = [clean_parent_phrase(x) for x in expr.split("×")]
    pieces = [x for x in pieces if x]
    if len(pieces) == 4:
        # 後尾の「の4体配合」等を除去
        pieces[-1] = re.sub(r"\s*の?4体配合.*$", "", pieces[-1]).strip()
        if all(p in name_to_no for p in pieces):
            c = build_candidate(
                result_no=child_no,
                result_name=child_name,
                parents=pieces,
                name_to_no=name_to_no,
                source_url=source_url,
                raw_text=raw_text,
                recipe_type="4体配合",
                note="左セル=子 / 右セル=4体配合式",
                source_context=source_context,
            )
            return [c] if c else []

    # 「4体配合」と書いてあるのに4親へ解けないものは安全側でREVIEW
    if "4体配合" in expr and len(pieces) != 4:
        # 2親として確定させない
        return []

    # 2体配合 + OR展開
    pairs = extract_pairs_from_fragment(expr, set(name_to_no.keys()))
    out = []
    for left, right, split_note in pairs:
        c = build_candidate(
            result_no=child_no,
            result_name=child_name,
            parents=[left, right],
            name_to_no=name_to_no,
            source_url=source_url,
            raw_text=raw_text,
            recipe_type="2体配合",
            note=("左セル=子 / 右セル=配合式" + (f" / {split_note}" if split_note else "")),
            source_context=source_context,
        )
        if c:
            out.append(c)
    return out


def parse_four_body_ui_row(
    tr: Tag,
    name_to_no: dict[str, int],
    page_result_no: int,
    page_result_name: str,
    source_url: str,
) -> RecipeCandidate | None:
    """
    v6:
    行単位ではなくtable全体から4体配合UIの4枠を直接取得。
    """
    table = tr.find_parent("table")
    return extract_four_body_from_ui_table(
        table, page_result_no, page_result_name, name_to_no, source_url
    )


def parse_pair_from_row(
    tr: Tag,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
) -> list[RecipeCandidate]:
    """
    v8:
    DIRECT_PATTERN / CREATION_ROUTE / USED_IN_BREEDING /
    GLOBAL_4BODY_TABLE / UNKNOWN を区別して子を決定する。
    """
    cells = tr.find_all(["td", "th"], recursive=False)
    if len(cells) < 2:
        return []

    context, heading = detect_source_context(tr, result_name)

    originals = [normalize_rule_text(node_text(c)) for c in cells]
    texts = [recipe_only_text(x) for x in originals]
    joined = " | ".join(texts)
    original_joined = " | ".join(originals)

    # UI見出し行
    if any(x in joined for x in (
        "配合早見表",
        "への配合パターン",
        "モンスター | 配合・入手方法",
        "親Aへの配合ルート",
        "親Bへの配合ルート",
    )):
        return []

    left_name = clean_parent_phrase(texts[0]) if texts else ""
    right_expr = texts[1] if len(texts) > 1 else ""

    def links(c: Tag) -> list[str]:
        return [
            monster_link_name(a)
            for a in c.find_all("a", href=True)
            if monster_link_name(a)
        ]

    # 1) ページ主対象への直接配合
    if context == SOURCE_DIRECT:
        table = tr.find_parent("table")
        table_text = normalize_rule_text(node_text(table)) if table else ""

        if "4体配合" in table_text:
            four = extract_four_body_from_ui_table(
                table, result_no, result_name, name_to_no, source_url, context
            )
            if four:
                return [four]

        p1 = classify_parent_token(texts[0], links(cells[0]), name_to_no)
        p2 = classify_parent_token(texts[1], links(cells[1]), name_to_no)
        if p1[2] and p2[2] and p1[2] not in UI_LABELS_4BODY and p2[2] not in UI_LABELS_4BODY:
            conf = "HIGH" if p1[0] != "review" and p2[0] != "review" else "REVIEW"
            return [RecipeCandidate(
                result_no=result_no,
                result_name=result_name,
                parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
                parent2_type=p2[0], parent2_no=p2[1], parent2=p2[2],
                confidence=conf,
                note=f"v8 DIRECT_PATTERN / {heading}",
                source_url=source_url,
                source_context=context,
                raw_text=original_joined,
            )]

    # 2) 作成ルート / 全体4体表
    if context in (SOURCE_ROUTE, SOURCE_GLOBAL_4BODY):
        if left_name in name_to_no:
            if "4体配合" in originals[1] or "×4" in originals[1] or "× 4" in originals[1]:
                four = parse_explicit_four_body_row(
                    left_name, originals[1], name_to_no, source_url,
                    original_joined, context
                )
                if four:
                    return [four]

            if "×" in right_expr:
                cands = parse_child_recipe_expression(
                    left_name, right_expr, name_to_no, source_url,
                    original_joined, context
                )
                if cands:
                    return cands

    # 3) 「Xを使う主な配合」
    # ここはページ主対象Xは親。表の子が明示されないケースは安全側で採用しない。
    if context == SOURCE_USED_IN:
        if len(texts) >= 3:
            child_candidate = clean_parent_phrase(texts[-1])
            if child_candidate in name_to_no:
                parent_names = []
                for c in cells[:-1]:
                    parent_names.extend(known_monsters_in_cell(c, name_to_no))
                parent_names = [p for p in parent_names if p != child_candidate]
                if len(parent_names) >= 2:
                    c = build_candidate(
                        result_no=name_to_no[child_candidate],
                        result_name=child_candidate,
                        parents=parent_names[:2],
                        name_to_no=name_to_no,
                        source_url=source_url,
                        raw_text=original_joined,
                        recipe_type="2体配合",
                        note=f"v8 USED_IN_BREEDING / {heading}",
                        confidence="HIGH",
                        source_context=context,
                    )
                    if c:
                        return [c]
        return []

    # 4) UNKNOWN
    # ページ主対象を子として推測しない。
    # ただし「左セル=子」「右セルに【4体配合】」が明示されている場合は、
    # 文脈不明でも構造自体は十分強いので4体1件として保持する。
    if context == SOURCE_UNKNOWN and left_name in name_to_no:
        if (
            "4体配合" in originals[1]
            or "×4" in originals[1]
            or "× 4" in originals[1]
        ):
            four = parse_explicit_four_body_row(
                left_name, originals[1], name_to_no, source_url,
                original_joined, SOURCE_ROUTE
            )
            if four:
                four.note = "v9 左セル=子 + 明示4体配合（作成ルート候補）"
                four.source_context = SOURCE_ROUTE
                return [four]

        if "×" in right_expr:
            cands = parse_child_recipe_expression(
                left_name, right_expr, name_to_no, source_url,
                original_joined, SOURCE_ROUTE
            )
            for c in cands:
                c.source_context = SOURCE_ROUTE
                c.note = "v9 左セル=子 + 配合式（作成ルート候補）"
            return cands

    return []


def parse_pair_from_text_line(
    text: str,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
) -> list[RecipeCandidate]:
    """
    v3: 構造境界を優先し、OR/複数配合を独立レコードへ展開。
    """
    out = []
    known_names = set(name_to_no.keys())

    for frag in split_recipe_fragments(text):
        # v5: 4体配合表記がある断片は通常の2体配合フォールバックへ流さない
        if "4体配合" in frag:
            continue

        for left, right, split_note in extract_pairs_from_fragment(frag, known_names):
            # 入手方法由来語が残ったものは安全側で捨てる
            bad = ("【入手方法】", "スカウト", "孵化", "入手でき", "他国マスター")
            if any(w in left or w in right for w in bad):
                continue

            p1 = classify_parent_token(
                left, [left] if left in name_to_no else [], name_to_no
            )
            p2 = classify_parent_token(
                right, [right] if right in name_to_no else [], name_to_no
            )
            if not p1[2] or not p2[2]:
                continue

            conf = "HIGH" if p1[0] != "review" and p2[0] != "review" else "REVIEW"
            note = split_note or "構造化テキスト抽出"

            out.append(RecipeCandidate(
                result_no=result_no,
                result_name=result_name,
                parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
                parent2_type=p2[0], parent2_no=p2[1], parent2=p2[2],
                confidence="REVIEW",
                note="v8 テキストフォールバック（文脈不明）: " + note,
                source_url=source_url,
                source_context=SOURCE_UNKNOWN,
                raw_text=frag,
            ))
    return out




def extract_direct_recipe_blocks_v13(raw_section: str, result_name: str) -> list[tuple[str, str]]:
    """
    v13:
    ページ主対象の直接配合領域を複数抽出する。

    戻り値:
      [("MAIN", segment), ("ADDITIONAL", segment), ...]

    MAIN:
      Xへの配合パターン
    ADDITIONAL:
      その他の配合パターン
      Xの配合パターン
    """
    t = normalize_rule_text(raw_section)
    markers: list[tuple[int, str, str]] = []

    # 主配合
    for pat in (
        f"{result_name}への配合パターン",
        f"{result_name}への配合パターン(特殊配合)",
        f"{result_name}への配合パターン（特殊配合）",
    ):
        i = t.find(pat)
        if i >= 0:
            markers.append((i, "MAIN", pat))
            break

    # 追加配合
    for m in re.finditer(r"その他の配合パターン", t):
        markers.append((m.start(), "ADDITIONAL", m.group(0)))

    # ダークキング型: 「Xの配合パターン」
    # 「Xへの配合パターン」とは区別する。
    patt = re.escape(result_name) + r"の配合パターン"
    for m in re.finditer(patt, t):
        markers.append((m.start(), "ADDITIONAL", m.group(0)))

    markers.sort(key=lambda x: x[0])
    if not markers:
        return []

    blocks: list[tuple[str, str]] = []
    for idx, (start, kind, marker_text) in enumerate(markers):
        body_start = start + len(marker_text)
        next_marker_start = markers[idx + 1][0] if idx + 1 < len(markers) else len(t)
        tail = t[body_start:next_marker_start]

        # このdirect領域の終了境界
        end_candidates = []
        for stop in (
            f"▼{result_name}までの作成ルートを見る▼",
            f"{result_name}までの作成ルート",
            f"{result_name}の入手方法",
            f"{result_name}を使う主な配合",
            "を使う主な配合",
            "固有スキル",
            "固有特技",
            "特性と効果",
        ):
            j = tail.find(stop)
            if j >= 0:
                end_candidates.append(j)

        if end_candidates:
            tail = tail[:min(end_candidates)]

        tail = cut_at_semantic_boundary(tail)
        tail = clean_text(tail)
        if tail:
            blocks.append((kind, tail))

    return blocks


def parse_isolated_direct_segment_v13(
    seg: str,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
    kind: str,
) -> list[RecipeCandidate]:
    """
    v13:
    すでに切り出されたdirect配合領域の中身から、配合種別を判定する。
    領域名だけで4体配合と決めない。
    """
    seg = cut_at_semantic_boundary(seg)
    if not seg:
        return []

    # 1) 相手問わず
    any_partner = parse_any_partner_direct(
        seg, result_no, result_name, name_to_no, source_url
    )
    if any_partner:
        for c in any_partner:
            c.note = f"v13 {kind} direct / 相手問わず"
            c.source_context = SOURCE_DIRECT
        return any_partner

    # 2) 系統指定
    family_pair = extract_direct_monster_family_pair(
        seg, result_no, result_name, name_to_no, source_url
    )
    if family_pair:
        for c in family_pair:
            c.note = f"v13 {kind} direct / 系統指定親"
            c.source_context = SOURCE_DIRECT
        return family_pair

    names = known_monsters_in_text_order(
        seg, name_to_no, exclude={result_name}
    )

    # 3) 4体配合
    # UIラベルまたは「4体配合」明示 + 4親がある場合のみ。
    four_ui = (
        len(names) >= 4
        and (
            (
                "親A" in seg
                and "親B" in seg
                and "親Aへの配合ルート" in seg
                and "親Bへの配合ルート" in seg
            )
            or "4体配合" in seg
        )
    )
    if four_ui:
        c = build_candidate(
            result_no=result_no,
            result_name=result_name,
            parents=names[:4],
            name_to_no=name_to_no,
            source_url=source_url,
            raw_text=seg,
            recipe_type="4体配合",
            note=f"v13 {kind} direct / 4体配合",
            confidence="HIGH",
            source_context=SOURCE_DIRECT,
        )
        return [c] if c else []

    # 4) 明示4体式
    if ("×4" in seg or "× 4" in seg or "【4体配合】" in seg) and "×" in seg:
        parents = expand_four_body_compact_expression(seg, name_to_no)
        if parents and len(parents) == 4:
            c = build_candidate(
                result_no=result_no,
                result_name=result_name,
                parents=parents,
                name_to_no=name_to_no,
                source_url=source_url,
                raw_text=seg,
                recipe_type="4体配合",
                note=f"v13 {kind} direct / 4体式",
                confidence="HIGH",
                source_context=SOURCE_DIRECT,
            )
            return [c] if c else []

    # 5) 通常2体
    if len(names) >= 2:
        c = build_candidate(
            result_no=result_no,
            result_name=result_name,
            parents=names[:2],
            name_to_no=name_to_no,
            source_url=source_url,
            raw_text=seg,
            recipe_type="2体配合",
            note=f"v13 {kind} direct / 2体配合",
            confidence="HIGH",
            source_context=SOURCE_DIRECT,
        )
        return [c] if c else []

    return []


def is_ui_fragment_candidate_v13(c: RecipeCandidate) -> bool:
    """
    v13:
    ") × 2" など、HTML/UIの断片が親名として混入した候補を除外する。
    """
    vals = [c.parent1, c.parent2, c.parent3, c.parent4]
    for v in vals:
        s = clean_text(v or "")
        if not s:
            continue
        if re.fullmatch(r"[\)\]】）\s]*[×xX]\s*\d+", s):
            return True
        if s in {") × 2", ")×2", "×2", "× 2"}:
            return True
    return False

def extract_additional_direct_sections(raw_section: str, result_name: str) -> list[str]:
    """v12: 「その他の配合パターン」を主対象の追加配合領域として抽出。"""
    t = normalize_rule_text(raw_section)
    out = []
    for m in re.finditer(r"その他の配合パターン", t):
        tail = t[m.end():]
        ends = []
        for pat in (rf"{re.escape(result_name)}を使う主な配合", r"を使う主な配合", r"固有スキル", r"固有特技"):
            x = re.search(pat, tail)
            if x: ends.append(x.start())
        seg = tail[:min(ends) if ends else len(tail)].strip()
        if seg: out.append(seg)
    return out

def classify_unresolved_v12(row: dict) -> str:
    raw = normalize_rule_text(row.get("raw_text",""))
    if row.get("confidence") == "REVIEW":
        return "D_UI_FRAGMENT" if re.search(r"\)\s*[×xX]\s*\d", raw) else "D_OTHER_REVIEW"
    if "相手問わず" in raw or "相手問わず" in str(row.get("parent2","")):
        return "B_ROUTE_ANY_PARTNER" if re.search(r"作成ルート|入手方法", raw) else "B_ANY_PARTNER"
    if "その他の配合パターン" in raw: return "C_ADDITIONAL_PATTERN"
    if any(x in raw for x in FAMILY_RULES): return "C_FAMILY_RELATED"
    return "D_OTHER_MEDIUM"



FAMILY_DISPLAY_ALIASES_V16 = {
    "スライム系": "スライム系",
    "ドラゴン系": "ドラゴン系",
    "まじゅう系": "魔獣系",
    "魔獣系": "魔獣系",
    "しぜん系": "自然系",
    "自然系": "自然系",
    "ぶっしつ系": "物質系",
    "物質系": "物質系",
    "あくま系": "悪魔系",
    "悪魔系": "悪魔系",
    "ゾンビ系": "ゾンビ系",
    "？？？系": "？？？系",
}

def classify_parent_token_v16(text_value, links, name_to_no):
    """
    v16:
    Altemaの表示上のひらがな系統名を正規のfamilyへ寄せてから分類。
    それ以外は既存classify_parent_tokenへ委譲。
    """
    s = clean_text(text_value or "")
    for shown, canonical in FAMILY_DISPLAY_ALIASES_V16.items():
        if shown in s:
            return ("family", "", canonical)
    return classify_parent_token(text_value, links, name_to_no)


def parse_direct_two_cell_rows_v16(sec, expected_no, expected_name, name_to_no, source_url):
    """
    主対象の「○○への配合パターン / 配合ルート」表を左右2セルとして直接読む。
    monster × monster / family / 相手問わず に対応。
    """
    out = []

    for table in sec.find_all("table"):
        tt = clean_text(table.get_text(" ", strip=True))

        if not (
            f"{expected_name}への配合パターン" in tt
            or f"{expected_name}への配合ルート" in tt
            or f"{expected_name}の配合パターン" in tt
        ):
            continue

        if f"{expected_name}を使う主な配合" in tt:
            continue

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) != 2:
                continue

            left = clean_text(cells[0].get_text(" ", strip=True))
            right = clean_text(cells[1].get_text(" ", strip=True))
            blob = left + " " + right

            if any(x in blob for x in (
                "親A", "親B", "親1", "親2",
                "配合早見表", "配合パターン", "配合ルート"
            )):
                continue

            p1 = classify_parent_token_v16(left, cells[0].find_all("a"), name_to_no)
            p2 = classify_parent_token_v16(right, cells[1].find_all("a"), name_to_no)

            if not p1[2] or not p2[2]:
                continue

            if p1[0] != "monster" and p2[0] != "monster":
                continue

            if p1[2] in {") × 2", ")×2", "×2", "× 2"}:
                continue
            if p2[2] in {") × 2", ")×2", "×2", "× 2"}:
                continue

            c = RecipeCandidate(
                result_no=expected_no,
                result_name=expected_name,
                parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
                parent2_type=p2[0], parent2_no=p2[1], parent2=p2[2],
                recipe_type="2体配合",
                confidence="HIGH",
                note="v16 主対象direct 2セル表を構造化取得",
                source_url=source_url,
                source_context=SOURCE_DIRECT,
                source_method=METHOD_DIRECT_STRUCTURED,
                is_fallback=False,
                fallback_reason="",
                raw_text=blob,
            )

            if not any(same_recipe_ordered(c, x) for x in out):
                out.append(c)

    return out



def node_summary_v17(node):
    if node is None:
        return {"tag":"","class":"","id":"","text":"","href":"","img_alt":"","img_src":""}
    tag=getattr(node,"name","") or ""
    classes=" ".join(node.get("class",[])) if hasattr(node,"get") else ""
    node_id=node.get("id","") if hasattr(node,"get") else ""
    txt=clean_text(node.get_text(" ",strip=True)) if hasattr(node,"get_text") else ""
    href=img_alt=img_src=""
    if hasattr(node,"find"):
        a=node.find("a")
        if a is not None: href=a.get("href","") or ""
        img=node.find("img")
        if img is not None:
            img_alt=img.get("alt","") or ""
            img_src=img.get("src","") or ""
    return {"tag":tag,"class":classes,"id":node_id,"text":txt,"href":href,"img_alt":img_alt,"img_src":img_src}

def find_direct_heading_nodes_v17(sec, expected_name):
    targets=(f"{expected_name}への配合パターン",f"{expected_name}への配合ルート",f"{expected_name}の配合パターン")
    hits=[]
    for node in sec.find_all(True):
        txt=clean_text(node.get_text(" ",strip=True))
        if any(t in txt for t in targets) and len(txt)<=220:
            hits.append(node)
    return hits

def probe_dom_around_heading_v17(sec, expected_no, expected_name, source_url):
    rows=[]
    for hi,node in enumerate(find_direct_heading_nodes_v17(sec,expected_name),1):
        s=node_summary_v17(node); ps=node_summary_v17(getattr(node,"parent",None))
        prevs=node_summary_v17(node.find_previous_sibling() if hasattr(node,"find_previous_sibling") else None)
        nexts=node_summary_v17(node.find_next_sibling() if hasattr(node,"find_next_sibling") else None)
        children=node.find_all(True,recursive=False) if hasattr(node,"find_all") else []
        if not children: children=[node]
        for ci,child in enumerate(children,1):
            cs=node_summary_v17(child)
            rows.append({
                "result_no":expected_no,"result_name":expected_name,"source_url":source_url,
                "hit_index":hi,"child_index":ci,
                "heading_tag":s["tag"],"heading_class":s["class"],"heading_id":s["id"],"heading_text":s["text"],
                "parent_tag":ps["tag"],"parent_class":ps["class"],"parent_id":ps["id"],"parent_text":ps["text"],
                "prev_tag":prevs["tag"],"prev_class":prevs["class"],"prev_text":prevs["text"],
                "next_tag":nexts["tag"],"next_class":nexts["class"],"next_text":nexts["text"],
                "child_tag":cs["tag"],"child_class":cs["class"],"child_id":cs["id"],"child_text":cs["text"],
                "child_href":cs["href"],"child_img_alt":cs["img_alt"],"child_img_src":cs["img_src"],
            })
    return rows

def infer_direct_candidate_from_dom_v17(sec, expected_no, expected_name, name_to_no, source_url):
    out=[]; seen=set()
    for hit in find_direct_heading_nodes_v17(sec,expected_name):
        scopes=[hit,getattr(hit,"parent",None)]
        if hasattr(hit,"find_next_sibling"): scopes.append(hit.find_next_sibling())
        for scope in scopes:
            if scope is None: continue
            st=clean_text(scope.get_text(" ",strip=True))
            if not st or f"{expected_name}を使う主な配合" in st: continue
            names=known_monsters_in_text_order(st,name_to_no,exclude={expected_name})
            fam=""
            for shown,canonical in FAMILY_DISPLAY_ALIASES_V16.items():
                if shown in st: fam=canonical; break
            anyp="相手問わず" in st
            p1=names[0] if names else ""; p2=fam if fam else ("相手問わず" if anyp else "")
            if p1 and p2:
                sig=(expected_no,p1,p2)
                if sig in seen: continue
                seen.add(sig)
                out.append({
                    "result_no":expected_no,"result_name":expected_name,
                    "candidate_parent1":p1,"candidate_parent2":p2,
                    "candidate_parent2_type":"family" if fam else "rule",
                    "reason":"DOM周辺に既知モンスター名とfamilyが共存" if fam else "DOM周辺に既知モンスター名と相手問わずが共存",
                    "scope_tag":getattr(scope,"name","") or "",
                    "scope_class":" ".join(scope.get("class",[])) if hasattr(scope,"get") else "",
                    "scope_text":st[:500],"source_url":source_url,
                })
    return out



def canonical_family_from_text_v18(s: str) -> str:
    s = clean_text(s or "")
    for shown, canonical in FAMILY_DISPLAY_ALIASES_V16.items():
        if shown in s:
            return canonical
    return ""


def parse_tableline_direct_v18(
    sec,
    expected_no: int,
    expected_name: str,
    name_to_no: dict[str, int],
    source_url: str,
):
    """
    v18:
    Altemaの主対象配合早見表(table.tableLine)を構造で読む。

    想定構造:
      tr: Xの配合早見表
      tr: X
      tr: Xへの配合パターン / Xへの配合ルート
      tr: 親情報  ← ここを読む

    見出し側に誤記があっても、
    ・tableLine
    ・「Xの配合早見表」
    ・子行にX
    が一致すれば主対象direct表として扱う。

    4体配合UIは既存v13系に任せ、ここでは2体配合だけ正式化する。
    """
    out = []
    audit = []

    accepted_names = set(NAME_ALIASES.get(expected_no, [expected_name]))
    accepted_names.add(expected_name)

    for table_index, table in enumerate(sec.select("table.tableLine"), start=1):
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue

        row_texts = [clean_text(r.get_text(" ", strip=True)) for r in rows]
        table_text = clean_text(table.get_text(" ", strip=True))

        # まず「主対象の配合早見表」であることを確認。
        has_breeding_title = any(
            ("配合早見表" in rt) and any(n in rt for n in accepted_names)
            for rt in row_texts[:3]
        )
        if not has_breeding_title:
            continue

        # 子モンスターそのものの行が存在することを確認。
        child_row_indexes = []
        for i, (row, rt) in enumerate(zip(rows, row_texts)):
            links = [
                monster_link_name(a)
                for a in row.find_all("a", href=True)
                if monster_link_name(a)
            ]
            exact_text = rt in accepted_names
            linked_child = any(name_to_no.get(n) == expected_no for n in links if n in name_to_no)
            if exact_text or linked_child:
                child_row_indexes.append(i)

        if not child_row_indexes:
            continue

        child_idx = child_row_indexes[0]

        # 子行より後ろにある「配合パターン / 配合ルート」見出しを探す。
        header_idx = None
        header_text = ""
        for i in range(child_idx + 1, len(rows)):
            rt = row_texts[i]
            if "配合パターン" in rt or "配合ルート" in rt:
                header_idx = i
                header_text = rt
                break

        if header_idx is None:
            continue

        # 見出し直後の実データ行を探す。
        parent_row = None
        parent_row_text = ""
        for i in range(header_idx + 1, len(rows)):
            rt = row_texts[i]
            if not rt:
                continue

            # 4体配合のUI見出し行はここでは処理しない。
            if any(x in rt for x in (
                "親A", "親B", "親1", "親2", "親3", "親4",
                "親Aへの配合ルート", "親Bへの配合ルート",
            )):
                continue

            # 次の別セクションへ入ったら終了。
            if (
                "までの作成ルート" in rt
                or "を使う主な配合" in rt
                or "入手方法" in rt
            ):
                break

            parent_row = rows[i]
            parent_row_text = rt
            break

        if parent_row is None:
            continue

        # 行内に登場する既知モンスター名を順番通り取得。
        names = known_monsters_in_text_order(
            parent_row_text,
            name_to_no,
            exclude=accepted_names,
        )

        family = canonical_family_from_text_v18(parent_row_text)
        any_partner = "相手問わず" in parent_row_text

        candidate = None
        interpretation = ""

        # monster × family
        if len(names) >= 1 and family:
            p1 = make_parent_token(names[0], name_to_no)
            candidate = RecipeCandidate(
                result_no=expected_no,
                result_name=expected_name,
                parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
                parent2_type="family", parent2_no="", parent2=family,
                recipe_type="2体配合",
                confidence="HIGH",
                note="v18 table.tableLine direct構造取得 / 系統指定親",
                source_url=source_url,
                source_context=SOURCE_DIRECT,
                source_method=METHOD_DIRECT_STRUCTURED,
                is_fallback=False,
                fallback_reason="",
                raw_text=parent_row_text,
            )
            interpretation = "monster_x_family"

        # monster × 相手問わず
        elif len(names) >= 1 and any_partner:
            p1 = make_parent_token(names[0], name_to_no)
            candidate = RecipeCandidate(
                result_no=expected_no,
                result_name=expected_name,
                parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
                parent2_type="rule", parent2_no="", parent2="相手問わず",
                recipe_type="2体配合",
                confidence="HIGH",
                note="v18 table.tableLine direct構造取得 / 相手問わず",
                source_url=source_url,
                source_context=SOURCE_DIRECT,
                source_method=METHOD_DIRECT_STRUCTURED,
                is_fallback=False,
                fallback_reason="",
                raw_text=parent_row_text,
            )
            interpretation = "monster_x_any"

        # monster × monster
        elif len(names) == 2:
            p1 = make_parent_token(names[0], name_to_no)
            p2 = make_parent_token(names[1], name_to_no)
            candidate = RecipeCandidate(
                result_no=expected_no,
                result_name=expected_name,
                parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
                parent2_type=p2[0], parent2_no=p2[1], parent2=p2[2],
                recipe_type="2体配合",
                confidence="HIGH",
                note="v18 table.tableLine direct構造取得 / 2モンスター",
                source_url=source_url,
                source_context=SOURCE_DIRECT,
                source_method=METHOD_DIRECT_STRUCTURED,
                is_fallback=False,
                fallback_reason="",
                raw_text=parent_row_text,
            )
            interpretation = "monster_x_monster"

        # 4親以上ある場合は既存4体配合parserへ任せる。
        elif len(names) >= 4:
            interpretation = "defer_four_body"

        else:
            interpretation = "unresolved"

        audit.append({
            "result_no": expected_no,
            "result_name": expected_name,
            "table_index": table_index,
            "table_class": " ".join(table.get("class", [])),
            "table_text": table_text[:800],
            "child_row_index": child_idx,
            "header_row_index": header_idx,
            "header_text": header_text,
            "parent_row_text": parent_row_text,
            "known_monsters": " | ".join(names),
            "family": family,
            "any_partner": "YES" if any_partner else "NO",
            "interpretation": interpretation,
            "candidate_parent1": candidate.parent1 if candidate else "",
            "candidate_parent2": candidate.parent2 if candidate else "",
            "source_url": source_url,
        })

        if candidate is not None:
            if not any(same_recipe_ordered(candidate, x) for x in out):
                out.append(candidate)

    return out, audit


def parse_creation_section(
    html: str,
    expected_no: int,
    expected_name: str,
    master: dict[int, dict],
    source_url: str,
) -> tuple[list[RecipeCandidate], dict]:
    soup = BeautifulSoup(html, "html.parser")
    page_no, title = page_identity(soup)

    accepted_names = NAME_ALIASES.get(expected_no, [expected_name])
    page_text_head = node_text(soup)[:3500]
    name_ok = any(n in title or n in page_text_head for n in accepted_names)
    no_ok = page_no is None or page_no == expected_no

    heading = find_creation_heading(soup, expected_name)
    if heading is None:
        return [], {
            "status": "NO_SECTION",
            "page_no": page_no or "",
            "title": title,
            "raw_section": "",
            "reason": "「配合表・作り方」セクションが見つからない",
            "identity_ok": name_ok and no_ok,
            "dom_probe_v17": [],
            "direct_candidate_probe_v17": [],
            "tableline_audit_v18": [],
        }

    nodes = section_nodes_after_heading(heading)
    sec = section_soup(nodes)
    raw_section = node_text(sec)

    name_to_no = {r["name"]: no for no, r in master.items()}
    # 表記差も別名として登録。
    for no, aliases in NAME_ALIASES.items():
        for a in aliases:
            name_to_no[a] = no

    dom_probe_v17 = probe_dom_around_heading_v17(sec, expected_no, expected_name, source_url)
    direct_candidate_probe_v17 = infer_direct_candidate_from_dom_v17(
        sec, expected_no, expected_name, name_to_no, source_url
    )

    found: list[RecipeCandidate] = []

    # 0) v13: 主配合・追加配合を複数のdirect領域として抽出し、
    # 各領域の中身から2体/4体/相手問わず/系統指定を個別判定する。
    direct_candidates: list[RecipeCandidate] = []
    for kind, direct_seg in extract_direct_recipe_blocks_v13(raw_section, expected_name):
        parsed = parse_isolated_direct_segment_v13(
            direct_seg, expected_no, expected_name,
            name_to_no, source_url, kind
        )
        for cand in parsed:
            if not any(same_recipe_ordered(cand, x) for x in direct_candidates):
                direct_candidates.append(cand)

    # 旧方式でしか拾えないページへの保険
    if not direct_candidates:
        direct_candidates = parse_direct_pattern_from_raw_section(
            raw_section, expected_no, expected_name, name_to_no, source_url
        )

    found.extend(direct_candidates)

    # v18: v17で特定したtable.tableLine構造を正式directとして解析。
    tableline_direct_v18, tableline_audit_v18 = parse_tableline_direct_v18(
        sec, expected_no, expected_name, name_to_no, source_url
    )
    for cand in tableline_direct_v18:
        if not any(same_recipe_ordered(cand, x) for x in found):
            found.append(cand)
        if not any(same_recipe_ordered(cand, x) for x in direct_candidates):
            direct_candidates.append(cand)

    # v16: 主対象directの左右2セルを直接構造化
    for cand in parse_direct_two_cell_rows_v16(
        sec, expected_no, expected_name, name_to_no, source_url
    ):
        if not any(same_recipe_ordered(cand, x) for x in found):
            found.append(cand)
        if not any(same_recipe_ordered(cand, x) for x in direct_candidates):
            direct_candidates.append(cand)

    # 1) table行から抽出
    row_candidates: list[RecipeCandidate] = []
    for tr in sec.find_all("tr"):
        cands = parse_pair_from_row(
            tr, expected_no, expected_name, name_to_no, source_url
        )
        row_candidates.extend(cands)

    # 主対象直接配合と完全一致する行候補だけ重複除去。
    # 作成ルートの別モンスター配合は残す。
    for cand in row_candidates:
        if is_ui_fragment_candidate_v13(cand):
            continue
        if any(same_recipe_ordered(cand, d) for d in direct_candidates):
            continue
        # 直接配合が確定済みなのに、同じ主対象をUNKNOWN/ROUTEとして
        # 2体へ誤分解した候補は採用しない。
        if (
            direct_candidates
            and cand.result_no == expected_no
            and cand.source_context != SOURCE_DIRECT
        ):
            continue
        found.append(cand)

    # 2) 「A × B」テキストの保険
    # tableで高信頼候補が1件も取れなかった場合のみ。
    if not any(x.confidence == "HIGH" for x in found):
        for s in sec.stripped_strings:
            cands = parse_pair_from_text_line(
                str(s), expected_no, expected_name, name_to_no, source_url
            )
            found.extend(cands)

    # 3) サーベルきつね型:
    # 1セル目にモンスターリンク、2セル目が「相手問わず」
    # v16: direct構造化取得が全く無い場合だけ最終手段として実行。
    has_direct_high_v16 = any(
        c.confidence == "HIGH"
        and getattr(c, "source_method", "") == METHOD_DIRECT_STRUCTURED
        for c in found
    )
    if not found and not has_direct_high_v16:
        links = [
            (monster_link_name(a), a)
            for a in sec.find_all("a", href=True)
            if monster_link_name(a)
        ]
        if "相手問わず" in raw_section:
            known = [n for n, _ in links if n in name_to_no and n != expected_name]
            if known:
                p = known[0]
                found.append(RecipeCandidate(
                    result_no=expected_no,
                    result_name=expected_name,
                    parent1_type="monster",
                    parent1_no=str(name_to_no[p]),
                    parent1=p,
                    parent2_type="rule",
                    parent2_no="",
                    parent2="相手問わず",
                    confidence="MEDIUM",
                    note="セクション内リンク＋相手問わずから補完",
                    source_url=source_url,
                    source_method=METHOD_LINK_ANY_FALLBACK,
                    is_fallback=True,
                    fallback_reason="構造化解析で候補を確定できず、セクション内リンクと「相手問わず」を組み合わせて補完",
                    raw_text=raw_section[:500],
                ))

    # 重複除去
    dedup = {}
    for x in found:
        sig = recipe_signature_candidate(x)
        old = dedup.get(sig)
        if old is None:
            dedup[sig] = x
        else:
            # HIGH > MEDIUM > REVIEW
            pri = {"HIGH": 3, "MEDIUM": 2, "REVIEW": 1}
            if pri.get(x.confidence, 0) > pri.get(old.confidence, 0):
                dedup[sig] = x

    found = [apply_provenance_v15(x) for x in dedup.values()]

    return found, {
        "status": "OK" if name_ok and no_ok else "IDENTITY_REVIEW",
        "page_no": page_no or "",
        "title": title,
        "raw_section": raw_section[:3000],
        "reason": "" if name_ok and no_ok else f"name_ok={name_ok}, no_ok={no_ok}",
        "identity_ok": name_ok and no_ok,
        "dom_probe_v17": dom_probe_v17,
        "direct_candidate_probe_v17": direct_candidate_probe_v17,
        "tableline_audit_v18": tableline_audit_v18,
    }


# -----------------------------
# 比較
# -----------------------------

def norm_parent(t: str, no: str, name: str) -> str:
    t = clean_text(t)
    no = clean_text(no)
    name = normalize_rule_text(name)
    if t == "monster" and no:
        return f"M:{int(no)}"
    if t == "monster" and name:
        return f"MN:{name}"
    if t == "family" and name:
        return f"F:{normalize_family_rule_text(name) or name}"
    if name:
        return f"R:{name}"
    return ""


def pair_canonical(parts: list[str]) -> tuple[str, ...]:
    # 2体配合は親順不同として比較。
    return tuple(sorted(x for x in parts if x))


def recipe_signature_candidate(x: RecipeCandidate) -> str:
    parts = [
        norm_parent(x.parent1_type, x.parent1_no, x.parent1),
        norm_parent(x.parent2_type, x.parent2_no, x.parent2),
        norm_parent(x.parent3_type, x.parent3_no, x.parent3),
        norm_parent(x.parent4_type, x.parent4_no, x.parent4),
    ]
    return f"{x.result_no}|{x.recipe_type}|" + "|".join(pair_canonical(parts))


def recipe_signature_current(r: dict) -> str:
    result_no = int(r.get("result_no") or 0)
    recipe_type = clean_text(r.get("recipe_type", "")) or "2体配合"
    parts = []
    for i in range(1, 5):
        parts.append(norm_parent(
            r.get(f"parent{i}_type", ""),
            r.get(f"parent{i}_no", ""),
            r.get(f"parent{i}", ""),
        ))
    return f"{result_no}|{recipe_type}|" + "|".join(pair_canonical(parts))


def candidate_to_row(x: RecipeCandidate, master: dict[int, dict]) -> dict:
    m = master[x.result_no]
    return {
        "result_no": x.result_no,
        "result_name": x.result_name,
        "result_rank": m.get("rank", ""),
        "result_family": m.get("family", ""),
        "result_size": m.get("size", ""),
        "recipe_type": x.recipe_type,
        "parent1_type": x.parent1_type,
        "parent1_no": x.parent1_no,
        "parent1": x.parent1,
        "parent2_type": x.parent2_type,
        "parent2_no": x.parent2_no,
        "parent2": x.parent2,
        "parent3_type": x.parent3_type,
        "parent3_no": x.parent3_no,
        "parent3": x.parent3,
        "parent4_type": x.parent4_type,
        "parent4_no": x.parent4_no,
        "parent4": x.parent4,
        "confidence": x.confidence,
        "note": x.note,
        "source_context": x.source_context,
        "source_method": x.source_method,
        "is_fallback": x.is_fallback,
        "fallback_reason": x.fallback_reason,
        "source": "Altema",
        "source_url": x.source_url,
        "source_page": "individual_monster_page",
        "signature": recipe_signature_candidate(x),
        "raw_text": x.raw_text,
    }


def current_brief(r: dict) -> str:
    ps = []
    for i in range(1, 5):
        v = clean_text(r.get(f"parent{i}", ""))
        if v:
            ps.append(v)
    return " × ".join(ps)


def candidate_brief(r: dict) -> str:
    ps = [r.get("parent1", ""), r.get("parent2", ""), r.get("parent3", ""), r.get("parent4", "")]
    return " × ".join(x for x in ps if x)


# -----------------------------
# CSV出力
# -----------------------------

def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    OUT.mkdir(exist_ok=True)

    master = read_master()
    current = read_current_breeding_from_app_zip()

    # v4.1:
    # main() 後半の「子判定検証レポート」でも使うため、
    # モンスター名 -> 図鑑No の辞書を main スコープで明示的に作る。
    name_to_no = {r["name"]: no for no, r in master.items()}
    for no, aliases in NAME_ALIASES.items():
        for alias in aliases:
            name_to_no[alias] = no

    print("アルテマ図鑑から個別ページURLを取得中...", flush=True)
    url_map, failures = build_url_map(master)

    all_rows: list[dict] = []
    raw_rows: list[dict] = []
    review_rows: list[dict] = []
    dom_probe_rows_v17: list[dict] = []
    direct_candidate_probe_rows_v17: list[dict] = []
    tableline_audit_rows_v18: list[dict] = []

    # 強制的に確認したい代表例。
    MUST_CHECK = {
        128: "サーベルきつね",
    }

    for no in range(1, 660):
        name = master[no]["name"]
        url = url_map.get(no, "")

        if not url:
            raw_rows.append({
                "no": no, "name": name, "source_url": "",
                "status": "URL_MISSING", "page_no": "", "title": "",
                "recipe_count": 0, "raw_section": "",
            })
            print(f"{no:03d}/659 {name}: URLなし", flush=True)
            continue

        try:
            html = get(url)
            candidates, meta = parse_creation_section(
                html, no, name, master, url
            )

            dom_probe_rows_v17.extend(meta.get("dom_probe_v17", []))
            direct_candidate_probe_rows_v17.extend(meta.get("direct_candidate_probe_v17", []))
            tableline_audit_rows_v18.extend(meta.get("tableline_audit_v18", []))

            rows = [candidate_to_row(x, master) for x in candidates]
            all_rows.extend(rows)

            raw_rows.append({
                "no": no,
                "name": name,
                "source_url": url,
                "status": meta["status"],
                "page_no": meta["page_no"],
                "title": meta["title"],
                "recipe_count": len(rows),
                "raw_section": meta["raw_section"],
            })

            if meta["status"] not in ("OK",):
                failures.append({
                    "no": no,
                    "name": name,
                    "stage": "PAGE_IDENTITY/SECTION",
                    "reason": meta["reason"] or meta["status"],
                    "source_url": url,
                })

            for r in rows:
                if r["confidence"] != "HIGH":
                    review_rows.append({
                        "no": no,
                        "name": name,
                        "reason": f"confidence={r['confidence']} / {r['note']}",
                        "recipe": candidate_brief(r),
                        "source_url": url,
                        "raw_text": r["raw_text"],
                    })

            if no in MUST_CHECK:
                expected_parent = "アルミラージ"
                expected_rule = "相手問わず"
                ok = any(
                    expected_parent in (r["parent1"], r["parent2"])
                    and expected_rule in (r["parent1"], r["parent2"])
                    for r in rows
                )
                if not ok:
                    review_rows.append({
                        "no": no,
                        "name": name,
                        "reason": "代表例チェックNG: アルミラージ × 相手問わず を取得できていない",
                        "recipe": "",
                        "source_url": url,
                        "raw_text": meta["raw_section"][:1200],
                    })

            print(
                f"{no:03d}/659 {name}: {len(rows)}件 "
                f"[{meta['status']}]",
                flush=True
            )

        except Exception as e:
            failures.append({
                "no": no,
                "name": name,
                "stage": "FETCH/PARSE",
                "reason": str(e)[:500],
                "source_url": url,
            })
            raw_rows.append({
                "no": no, "name": name, "source_url": url,
                "status": "ERROR", "page_no": "", "title": "",
                "recipe_count": 0, "raw_section": str(e)[:1000],
            })
            print(f"{no:03d}/659 {name}: ERROR {e}", flush=True)

        time.sleep(REQUEST_INTERVAL)

    # 個別ページ取得結果
    recipe_fields = [
        "result_no","result_name","result_rank","result_family","result_size",
        "recipe_type",
        "parent1_type","parent1_no","parent1",
        "parent2_type","parent2_no","parent2",
        "parent3_type","parent3_no","parent3",
        "parent4_type","parent4_no","parent4",
        "confidence","note","source","source_url","source_page",
        "signature","raw_text",
    ]
    write_csv(
        OUT / "altema_individual_breeding_659.csv",
        sorted(all_rows, key=lambda r: (int(r["result_no"]), r["signature"])),
        recipe_fields,
    )

    # 現行CSVとの比較
    current_by_sig = {recipe_signature_current(r): r for r in current}
    new_by_sig = {r["signature"]: r for r in all_rows}

    diffs = []

    for sig, r in new_by_sig.items():
        if sig in current_by_sig:
            status = "一致"
            cur = current_by_sig[sig]
        else:
            status = "個別ページで新規発見"
            cur = {}
        diffs.append({
            "status": status,
            "result_no": r["result_no"],
            "result_name": r["result_name"],
            "individual_recipe": candidate_brief(r),
            "current_recipe": current_brief(cur) if cur else "",
            "confidence": r["confidence"],
            "source_url": r["source_url"],
            "signature": sig,
        })

    for sig, r in current_by_sig.items():
        if sig in new_by_sig:
            continue
        diffs.append({
            "status": "現行CSVにのみ存在",
            "result_no": r.get("result_no", ""),
            "result_name": r.get("result_name", ""),
            "individual_recipe": "",
            "current_recipe": current_brief(r),
            "confidence": "",
            "source_url": r.get("source_page", "") or r.get("source", ""),
            "signature": sig,
        })

    diff_fields = [
        "status","result_no","result_name",
        "individual_recipe","current_recipe",
        "confidence","source_url","signature",
    ]
    status_order = {"個別ページで新規発見": 0, "現行CSVにのみ存在": 1, "一致": 2}
    diffs.sort(key=lambda r: (
        status_order.get(r["status"], 9),
        int(r["result_no"]) if str(r["result_no"]).isdigit() else 9999,
        r["signature"],
    ))
    write_csv(
        OUT / "altema_breeding_diff_vs_current.csv",
        diffs,
        diff_fields,
    )

    write_csv(
        OUT / "altema_breeding_fetch_failures.csv",
        failures,
        ["no","name","stage","reason","source_url"],
    )

    # v3: REVIEWを原因別に分類
    def review_reason_v3(r):
        raw = r.get("raw_text", "")
        recipe = r.get("recipe", "")
        if any(x in raw for x in ("の入手方法","までの作成ルート","を使う主な配合")):
            return "境界跨ぎ疑い"
        if "4体配合" in r.get("reason","") or "4体配合" in recipe:
            return "4体配合要確認"
        if any(m in raw for m in NON_RECIPE_MARKERS):
            return "入手方法境界"
        if raw.count("×") > 1:
            return "複数配合連結"
        if re.search(r"\b(?:or|OR)\b|または|もしくは|あるいは", raw):
            return "OR条件"
        if "/" in raw or "／" in raw:
            return "複数候補"
        if " / " in recipe:
            return "親候補曖昧"
        return "その他"

    for r in review_rows:
        r["review_category"] = review_reason_v3(r)

    write_csv(
        OUT / "altema_breeding_review_needed.csv",
        review_rows,
        ["no","name","review_category","reason","recipe","source_context","source_url","raw_text"],
    )

    review_category_counts = {}
    for r in review_rows:
        k = r["review_category"]
        review_category_counts[k] = review_category_counts.get(k, 0) + 1

    write_csv(
        OUT / "altema_breeding_review_summary.csv",
        [{"review_category": k, "count": v}
         for k, v in sorted(review_category_counts.items(), key=lambda x: (-x[1], x[0]))],
        ["review_category","count"],
    )

    write_csv(
        OUT / "altema_breeding_raw_sections.csv",
        raw_rows,
        ["no","name","source_url","status","page_no","title","recipe_count","raw_section"],
    )

    # ---- v7: 4体配合の重複除去 ----
    # 順序を無視して重複除去すると、
    # A,B,A,B と A,A,B,B のような「構成は同じだが並び方が違う」ケースを
    # 消してしまうため、v7では原順序まで一致したものだけを重複除去する。
    deduped = []
    seen_4_ordered = set()
    for r in all_rows:
        if r.get("recipe_type") == "4体配合":
            sig = four_body_ordered_signature(r)
            if sig in seen_4_ordered:
                continue
            seen_4_ordered.add(sig)
        deduped.append(r)
    all_rows = deduped

    # ---- v5: 4体配合の専用検証レポート ----
    v5_four_body_validation = []
    for r in all_rows:
        if r.get("recipe_type") != "4体配合":
            continue
        parents = [
            r.get("parent1",""), r.get("parent2",""),
            r.get("parent3",""), r.get("parent4",""),
        ]
        parent_count = sum(1 for p in parents if clean_text(p))
        v5_four_body_validation.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "parent_count": parent_count,
            "parent1": parents[0],
            "parent2": parents[1],
            "parent3": parents[2],
            "parent4": parents[3],
            "confidence": r.get("confidence",""),
            "note": r.get("note",""),
            "raw_text": r.get("raw_text",""),
            "source_url": r.get("source_url",""),
            "valid_4body": "YES" if parent_count == 4 else "NO",
        })

    write_csv(
        OUT / "altema_breeding_v5_four_body_validation.csv",
        v5_four_body_validation,
        [
            "result_no","result_name","parent_count",
            "parent1","parent2","parent3","parent4",
            "confidence","note","raw_text","source_url","valid_4body"
        ],
    )

    # ---- v9: 主対象直接配合の代表例検証 ----
    must_names = {
        "スラキャンサー",
        "魔王ウルノーガ",
        "大魔王マデュラージャ",
        "キングモーモン",
        "シャイニング",
        "ブリザード",
    }
    v9_focus = []
    for r in all_rows:
        if r.get("result_name") not in must_names:
            continue
        v9_focus.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "source_context": r.get("source_context", SOURCE_UNKNOWN),
            "recipe_type": r.get("recipe_type",""),
            "parent1": r.get("parent1",""),
            "parent2": r.get("parent2",""),
            "parent3": r.get("parent3",""),
            "parent4": r.get("parent4",""),
            "confidence": r.get("confidence",""),
            "note": r.get("note",""),
            "source_url": r.get("source_url",""),
            "raw_text": r.get("raw_text",""),
        })

    write_csv(
        OUT / "altema_breeding_v9_focus_validation.csv",
        v9_focus,
        [
            "result_no","result_name","source_context","recipe_type",
            "parent1","parent2","parent3","parent4",
            "confidence","note","source_url","raw_text"
        ],
    )

    # ---- v18: table.tableLine direct構造の正式解析監査 ----
    write_csv(
        OUT / "altema_breeding_v18_tableline_audit.csv",
        tableline_audit_rows_v18,
        [
            "result_no","result_name","table_index","table_class","table_text",
            "child_row_index","header_row_index","header_text","parent_row_text",
            "known_monsters","family","any_partner","interpretation",
            "candidate_parent1","candidate_parent2","source_url"
        ],
    )

    focus_names_v18 = {
        "おばけキャンドル","カバシラー","かまっち","ブラウニー",
        "デザートデーモン","あくまのカガミ","とつげきうお",
        "ジェネラルダンテ","ヘルダイバー","ガスダンゴ",
    }
    focus_rows_v18 = []
    for r in all_rows:
        if r.get("result_name") not in focus_names_v18:
            continue
        focus_rows_v18.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "parent1_type": r.get("parent1_type",""),
            "parent1": r.get("parent1",""),
            "parent2_type": r.get("parent2_type",""),
            "parent2": r.get("parent2",""),
            "confidence": r.get("confidence",""),
            "source_method": r.get("source_method",""),
            "is_fallback": r.get("is_fallback",False),
            "note": r.get("note",""),
            "raw_text": r.get("raw_text",""),
            "source_url": r.get("source_url",""),
        })

    write_csv(
        OUT / "altema_breeding_v18_focus_validation.csv",
        focus_rows_v18,
        [
            "result_no","result_name",
            "parent1_type","parent1","parent2_type","parent2",
            "confidence","source_method","is_fallback",
            "note","raw_text","source_url"
        ],
    )

    # ---- v17: DOM構造診断 ----
    write_csv(
        OUT / "altema_breeding_v17_dom_probe.csv",
        dom_probe_rows_v17,
        ["result_no","result_name","source_url","hit_index","child_index",
         "heading_tag","heading_class","heading_id","heading_text",
         "parent_tag","parent_class","parent_id","parent_text",
         "prev_tag","prev_class","prev_text","next_tag","next_class","next_text",
         "child_tag","child_class","child_id","child_text","child_href","child_img_alt","child_img_src"],
    )
    write_csv(
        OUT / "altema_breeding_v17_direct_candidate_probe.csv",
        direct_candidate_probe_rows_v17,
        ["result_no","result_name","candidate_parent1","candidate_parent2","candidate_parent2_type",
         "reason","scope_tag","scope_class","scope_text","source_url"],
    )

    # ---- v16: C群10体 direct化重点検証 ----
    focus_names_v16 = {
        "おばけキャンドル","カバシラー","かまっち","ブラウニー",
        "デザートデーモン","あくまのカガミ","とつげきうお",
        "ジェネラルダンテ","ヘルダイバー","ガスダンゴ",
    }

    focus_v16 = []
    for r in all_rows:
        if r.get("result_name") not in focus_names_v16:
            continue
        focus_v16.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "parent1_type": r.get("parent1_type",""),
            "parent1": r.get("parent1",""),
            "parent2_type": r.get("parent2_type",""),
            "parent2": r.get("parent2",""),
            "confidence": r.get("confidence",""),
            "source_method": r.get("source_method",""),
            "is_fallback": r.get("is_fallback",False),
            "note": r.get("note",""),
            "raw_text": r.get("raw_text",""),
            "source_url": r.get("source_url",""),
        })

    write_csv(
        OUT / "altema_breeding_v16_direct_two_cell_focus.csv",
        focus_v16,
        [
            "result_no","result_name",
            "parent1_type","parent1","parent2_type","parent2",
            "confidence","source_method","is_fallback",
            "note","raw_text","source_url"
        ],
    )

    # ---- v15: 取得経路 / fallback監査 ----
    provenance_rows_v15 = [provenance_row_v15(r) for r in all_rows]
    write_csv(
        OUT / "altema_breeding_v15_provenance_audit.csv",
        provenance_rows_v15,
        [
            "result_no","result_name","recipe_type",
            "parent1","parent2","parent3","parent4",
            "confidence","source_context","source_method",
            "is_fallback","fallback_reason","note","source_url","raw_text"
        ],
    )

    fallback_rows_v15 = [
        provenance_row_v15(r)
        for r in all_rows
        if bool(r.get("is_fallback", False))
    ]
    write_csv(
        OUT / "altema_breeding_v15_fallback_only.csv",
        fallback_rows_v15,
        [
            "result_no","result_name","recipe_type",
            "parent1","parent2","parent3","parent4",
            "confidence","source_context","source_method",
            "is_fallback","fallback_reason","note","source_url","raw_text"
        ],
    )

    method_counts_v15 = {}
    for r in all_rows:
        method = str(r.get("source_method","UNKNOWN"))
        method_counts_v15[method] = method_counts_v15.get(method, 0) + 1

    write_csv(
        OUT / "altema_breeding_v15_source_method_summary.csv",
        [
            {"source_method": k, "count": v}
            for k, v in sorted(method_counts_v15.items(), key=lambda x: (-x[1], x[0]))
        ],
        ["source_method","count"],
    )

    # ---- v13: 複数direct領域の重点検証 ----
    focus_names_v13 = {"メタルカイザー", "ダークキング"}
    focus_v13 = []
    for r in all_rows:
        if r.get("result_name") not in focus_names_v13:
            continue
        focus_v13.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "recipe_type": r.get("recipe_type",""),
            "parent1": r.get("parent1",""),
            "parent2": r.get("parent2",""),
            "parent3": r.get("parent3",""),
            "parent4": r.get("parent4",""),
            "confidence": r.get("confidence",""),
            "source_context": r.get("source_context", SOURCE_UNKNOWN),
            "note": r.get("note",""),
            "raw_text": r.get("raw_text",""),
            "source_url": r.get("source_url",""),
        })

    write_csv(
        OUT / "altema_breeding_v13_multi_direct_focus.csv",
        focus_v13,
        [
            "result_no","result_name","recipe_type",
            "parent1","parent2","parent3","parent4",
            "confidence","source_context","note","raw_text","source_url"
        ],
    )

    # ---- v12: MEDIUM / REVIEW の分類 ----
    unresolved_v12 = []
    for r in all_rows:
        if r.get("confidence") in ("MEDIUM", "REVIEW"):
            x = dict(r)
            x["v12_class"] = classify_unresolved_v12(r)
            unresolved_v12.append(x)
    if unresolved_v12:
        fields = ["v12_class"] + [k for k in unresolved_v12[0] if k != "v12_class"]
        write_csv(OUT / "altema_breeding_v12_unresolved_classification.csv", unresolved_v12, fields)

    # v12: 同一の子に複数の異なる配合が保持されたか確認
    grouped_v12 = defaultdict(list)
    for r in all_rows:
        grouped_v12[str(r.get("result_no",""))].append(r)
    multi_v12 = []
    for arr in grouped_v12.values():
        sigs = {(
            r.get("recipe_type",""),
            r.get("parent1_type",""),r.get("parent1",""),
            r.get("parent2_type",""),r.get("parent2",""),
            r.get("parent3_type",""),r.get("parent3",""),
            r.get("parent4_type",""),r.get("parent4","")
        ) for r in arr}
        if len(sigs) >= 2:
            for r in arr:
                x=dict(r); x["recipe_count_for_result"]=len(sigs); multi_v12.append(x)
    if multi_v12:
        fields = ["recipe_count_for_result"] + [k for k in multi_v12[0] if k != "recipe_count_for_result"]
        write_csv(OUT / "altema_breeding_v12_multi_recipe_validation.csv", multi_v12, fields)

    # ---- v11: 系統指定親の重点検証 ----
    family_rows = []
    for r in all_rows:
        atoms = recipe_parent_atoms(r)
        if not any(a[0] == "family" for a in atoms):
            continue
        family_rows.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "recipe_type": r.get("recipe_type",""),
            "parent1_type": r.get("parent1_type",""),
            "parent1": r.get("parent1",""),
            "parent2_type": r.get("parent2_type",""),
            "parent2": r.get("parent2",""),
            "confidence": r.get("confidence",""),
            "source_context": r.get("source_context", SOURCE_UNKNOWN),
            "note": r.get("note",""),
            "raw_text": r.get("raw_text",""),
            "source_url": r.get("source_url",""),
        })

    write_csv(
        OUT / "altema_breeding_v11_family_parent_validation.csv",
        family_rows,
        [
            "result_no","result_name","recipe_type",
            "parent1_type","parent1","parent2_type","parent2",
            "confidence","source_context","note","raw_text","source_url"
        ],
    )

    # ---- v10: 境界認識の重点検証 ----
    boundary_checks = []
    for r in all_rows:
        if r.get("result_name") not in {
            "シャイニング",
            "ブリザード",
            "キングモーモン",
            "スラキャンサー",
            "魔王ウルノーガ",
            "大魔王マデュラージャ",
        }:
            continue

        raw = r.get("raw_text","")
        boundary_checks.append({
            "result_no": r.get("result_no",""),
            "result_name": r.get("result_name",""),
            "recipe_type": r.get("recipe_type",""),
            "parent1": r.get("parent1",""),
            "parent2": r.get("parent2",""),
            "parent3": r.get("parent3",""),
            "parent4": r.get("parent4",""),
            "source_context": r.get("source_context", SOURCE_UNKNOWN),
            "contains_boundary_text": (
                "YES"
                if any(x in raw for x in ("の入手方法","までの作成ルート","を使う主な配合"))
                else "NO"
            ),
            "is_suspicious_shining_duplicate": (
                "YES"
                if r.get("result_name") == "シャイニング"
                and r.get("parent1") == "ブリザード"
                and r.get("parent2") == "ブリザード"
                else "NO"
            ),
            "confidence": r.get("confidence",""),
            "note": r.get("note",""),
            "raw_text": raw,
            "source_url": r.get("source_url",""),
        })

    write_csv(
        OUT / "altema_breeding_v10_boundary_validation.csv",
        boundary_checks,
        [
            "result_no","result_name","recipe_type",
            "parent1","parent2","parent3","parent4",
            "source_context","contains_boundary_text",
            "is_suspicious_shining_duplicate",
            "confidence","note","raw_text","source_url"
        ],
    )

    # ---- v8: source_context 検証 ----
    ctx_counts = {}
    for r in all_rows:
        ctx = r.get("source_context", SOURCE_UNKNOWN)
        ctx_counts[ctx] = ctx_counts.get(ctx, 0) + 1

    write_csv(
        OUT / "altema_breeding_v8_source_context_summary.csv",
        [
            {"source_context": k, "count": v}
            for k, v in sorted(ctx_counts.items(), key=lambda x: (-x[1], x[0]))
        ],
        ["source_context","count"],
    )

    write_csv(
        OUT / "altema_breeding_v8_context_validation.csv",
        [
            {
                "result_no": r.get("result_no",""),
                "result_name": r.get("result_name",""),
                "source_context": r.get("source_context", SOURCE_UNKNOWN),
                "recipe_type": r.get("recipe_type",""),
                "recipe": current_brief(r),
                "confidence": r.get("confidence",""),
                "note": r.get("note",""),
                "source_url": r.get("source_url",""),
                "raw_text": r.get("raw_text",""),
            }
            for r in all_rows
        ],
        [
            "result_no","result_name","source_context","recipe_type",
            "recipe","confidence","note","source_url","raw_text"
        ],
    )

    # ---- v7: 4体配合の原順序・2+2構造・構成を分離した検証レポート ----
    v7_rows = []
    four_by_result = {}

    for r in all_rows:
        if r.get("recipe_type") != "4体配合":
            continue

        parents = [clean_text(r.get(f"parent{i}", "")) for i in range(1, 5)]
        ordered_sig = " > ".join(parents)
        pairing_sig = (
            f"({parents[0]} + {parents[1]}) | "
            f"({parents[2]} + {parents[3]})"
        )
        composition_sig = " | ".join(sorted(parents))
        composition_text = four_body_composition_text(r)

        row = {
            "result_no": r.get("result_no", ""),
            "result_name": r.get("result_name", ""),
            "parent1": parents[0],
            "parent2": parents[1],
            "parent3": parents[2],
            "parent4": parents[3],
            "ordered_signature": ordered_sig,
            "pairing_signature": pairing_sig,
            "composition_signature": composition_sig,
            "composition_text": composition_text,
            "confidence": r.get("confidence", ""),
            "note": r.get("note", ""),
            "source_url": r.get("source_url", ""),
            "raw_text": r.get("raw_text", ""),
        }
        v7_rows.append(row)
        four_by_result.setdefault(int(r.get("result_no") or 0), []).append(r)

    write_csv(
        OUT / "altema_breeding_v7_four_body_order_and_composition.csv",
        v7_rows,
        [
            "result_no","result_name",
            "parent1","parent2","parent3","parent4",
            "ordered_signature","pairing_signature",
            "composition_signature","composition_text",
            "confidence","note","source_url","raw_text"
        ],
    )

    # 同じ子について「4体構成は同じだが原順序/2+2構造が違う」候補を抽出
    order_conflicts = []
    for result_no, arr in four_by_result.items():
        for i in range(len(arr)):
            for j in range(i + 1, len(arr)):
                a, b = arr[i], arr[j]
                if four_body_composition_signature(a) != four_body_composition_signature(b):
                    continue

                same_order = four_body_ordered_signature(a) == four_body_ordered_signature(b)
                same_pairing = four_body_pairing_signature(a) == four_body_pairing_signature(b)

                if same_order:
                    continue

                order_conflicts.append({
                    "result_no": result_no,
                    "result_name": a.get("result_name", "") or b.get("result_name", ""),
                    "composition": four_body_composition_text(a),
                    "recipe_a_order": " > ".join(clean_text(a.get(f"parent{k}", "")) for k in range(1, 5)),
                    "recipe_b_order": " > ".join(clean_text(b.get(f"parent{k}", "")) for k in range(1, 5)),
                    "same_pairing": "YES" if same_pairing else "NO",
                    "classification": (
                        "順序のみ相違"
                        if same_pairing
                        else "4体構成一致・2+2組み分け相違"
                    ),
                    "source_a": a.get("source_url", ""),
                    "source_b": b.get("source_url", ""),
                    "raw_a": a.get("raw_text", ""),
                    "raw_b": b.get("raw_text", ""),
                })

    write_csv(
        OUT / "altema_breeding_v7_four_body_order_conflicts.csv",
        order_conflicts,
        [
            "result_no","result_name","composition",
            "recipe_a_order","recipe_b_order","same_pairing",
            "classification","source_a","source_b","raw_a","raw_b"
        ],
    )

    # ---- v4: 子判定・4体配合の検証レポート ----
    v4_validation = []
    for r in all_rows:
        raw = r.get("raw_text", "")
        if " | " in raw and "×" in raw:
            left = clean_parent_phrase(raw.split(" | ", 1)[0])
            if left in name_to_no:
                v4_validation.append({
                    "result_no": r.get("result_no",""),
                    "result_name": r.get("result_name",""),
                    "raw_left": left,
                    "left_is_known_monster": "YES",
                    "child_matches_left": "YES" if r.get("result_name","") == left else "NO",
                    "recipe_type": r.get("recipe_type",""),
                    "recipe": current_brief(r),
                    "raw_text": raw,
                    "source_url": r.get("source_url",""),
                })

    write_csv(
        OUT / "altema_breeding_v4_child_validation.csv",
        v4_validation,
        [
            "result_no","result_name","raw_left","left_is_known_monster",
            "child_matches_left","recipe_type","recipe","raw_text","source_url"
        ],
    )

    # ---- v2: 一般ルールによる具体例の包含候補を解析 ----
    # 現行 + 個別ページ取得結果を同じ形へ寄せて、冗長具体例を「削除候補」として出す。
    merged_for_cover = []

    for r in all_rows:
        x = dict(r)
        x["origin"] = "individual"
        merged_for_cover.append(x)

    for r in current:
        x = {
            "result_no": int(r.get("result_no") or 0),
            "result_name": r.get("result_name", ""),
            "recipe_type": clean_text(r.get("recipe_type", "")) or "2体配合",
            "parent1_type": r.get("parent1_type", ""),
            "parent1_no": r.get("parent1_no", ""),
            "parent1": r.get("parent1", ""),
            "parent2_type": r.get("parent2_type", ""),
            "parent2_no": r.get("parent2_no", ""),
            "parent2": r.get("parent2", ""),
            "parent3_type": r.get("parent3_type", ""),
            "parent3_no": r.get("parent3_no", ""),
            "parent3": r.get("parent3", ""),
            "parent4_type": r.get("parent4_type", ""),
            "parent4_no": r.get("parent4_no", ""),
            "parent4": r.get("parent4", ""),
            "origin": "current",
            "source_url": r.get("source_page", "") or r.get("source", ""),
        }
        merged_for_cover.append(x)

    by_result = {}
    for r in merged_for_cover:
        by_result.setdefault(int(r["result_no"]), []).append(r)

    containment_rows = []
    seen_pairs = set()
    for result_no, arr in by_result.items():
        generals = [r for r in arr if any(a[0] in ("rule", "family") for a in recipe_parent_atoms(r))]
        specifics = [r for r in arr if all(a[0] == "monster" for a in recipe_parent_atoms(r)) and len(recipe_parent_atoms(r)) == 2]

        for g in generals:
            for s in specifics:
                if g is s:
                    continue
                if pair_rule_covers_specific(g, s, master):
                    key = (
                        result_no,
                        recipe_signature_current(g),
                        recipe_signature_current(s),
                        g.get("origin",""),
                        s.get("origin",""),
                    )
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    containment_rows.append({
                        "result_no": result_no,
                        "result_name": master[result_no]["name"],
                        "general_origin": g.get("origin",""),
                        "general_recipe": current_brief(g),
                        "specific_origin": s.get("origin",""),
                        "specific_recipe": current_brief(s),
                        "action_candidate": "具体例を統合候補",
                        "reason": "一般ルールが具体例を包含",
                        "general_source": g.get("source_url",""),
                    })

    write_csv(
        OUT / "altema_breeding_containment_candidates.csv",
        containment_rows,
        [
            "result_no","result_name",
            "general_origin","general_recipe",
            "specific_origin","specific_recipe",
            "action_candidate","reason","general_source"
        ],
    )

    # サマリー
    counts = {}
    for r in diffs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    result_with_recipe = len(set(int(r["result_no"]) for r in all_rows))
    high = sum(1 for r in all_rows if r["confidence"] == "HIGH")
    medium = sum(1 for r in all_rows if r["confidence"] == "MEDIUM")
    review = sum(1 for r in all_rows if r["confidence"] == "REVIEW")
    fallback_count_v15 = sum(1 for r in all_rows if bool(r.get("is_fallback", False)))
    link_any_fallback_count_v15 = sum(
        1 for r in all_rows
        if r.get("source_method") == METHOD_LINK_ANY_FALLBACK
    )
    text_fallback_count_v15 = sum(
        1 for r in all_rows
        if r.get("source_method") == METHOD_TEXT_FALLBACK
    )
    family_parent_count = sum(
        1 for r in all_rows
        if any(a[0] == "family" for a in recipe_parent_atoms(r))
    )
    family_parent_high_count = sum(
        1 for r in all_rows
        if r.get("confidence") == "HIGH"
        and any(a[0] == "family" for a in recipe_parent_atoms(r))
    )
    direct_count = sum(1 for r in all_rows if r.get("source_context") == SOURCE_DIRECT)
    route_count = sum(1 for r in all_rows if r.get("source_context") == SOURCE_ROUTE)
    used_count = sum(1 for r in all_rows if r.get("source_context") == SOURCE_USED_IN)
    global4_count = sum(1 for r in all_rows if r.get("source_context") == SOURCE_GLOBAL_4BODY)
    unknown_count = sum(1 for r in all_rows if r.get("source_context") == SOURCE_UNKNOWN)
    four_body_count = sum(1 for r in all_rows if r.get("recipe_type") == "4体配合")
    valid_four_body_count = sum(
        1 for r in all_rows
        if r.get("recipe_type") == "4体配合"
        and all(clean_text(r.get(f"parent{i}","")) for i in range(1,5))
    )
    compact_four_body_count = sum(
        1 for r in all_rows
        if r.get("recipe_type") == "4体配合"
        and ("×4" in r.get("raw_text","") or "× 4" in r.get("raw_text","") or ")×2" in r.get("raw_text",""))
    )
    four_body_order_conflict_count = len(order_conflicts)
    child_left_count = sum(
        1 for r in all_rows
        if " | " in r.get("raw_text","")
        and "×" in r.get("raw_text","")
        and r.get("result_name","") == clean_parent_phrase(r.get("raw_text","").split(" | ",1)[0])
    )
    acquisition_like = sum(
        1 for r in all_rows
        if any(w in (r.get("parent1","") + " " + r.get("parent2",""))
               for w in ("【入手方法】","スカウト","孵化","他国マスターから"))
    )

    saber_ok = any(
        int(r["result_no"]) == 128
        and "アルミラージ" in (r["parent1"], r["parent2"])
        and "相手問わず" in (r["parent1"], r["parent2"])
        for r in all_rows
    )

    summary = [
        {"item": "個別ページ解析対象", "value": "659"},
        {"item": "配合取得総件数", "value": str(len(all_rows))},
        {"item": "配合を1件以上取得できたモンスター数", "value": str(result_with_recipe)},
        {"item": "HIGH confidence", "value": str(high)},
        {"item": "MEDIUM confidence", "value": str(medium)},
        {"item": "REVIEW confidence", "value": str(review)},
        {"item": "現行CSV件数", "value": str(len(current))},
        {"item": "一致", "value": str(counts.get("一致", 0))},
        {"item": "個別ページで新規発見", "value": str(counts.get("個別ページで新規発見", 0))},
        {"item": "現行CSVにのみ存在", "value": str(counts.get("現行CSVにのみ存在", 0))},
        {"item": "取得/照合失敗", "value": str(len(failures))},
        {"item": "要確認候補", "value": str(len(review_rows))},
        {"item": "fallback使用件数", "value": str(fallback_count_v15)},
        {"item": "FALLBACK_LINK_ANY", "value": str(link_any_fallback_count_v15)},
        {"item": "TEXT_FALLBACK", "value": str(text_fallback_count_v15)},
        {"item": "系統指定親を含む配合", "value": str(family_parent_count)},
        {"item": "系統指定親 HIGH", "value": str(family_parent_high_count)},
        {"item": "DIRECT_PATTERN", "value": str(direct_count)},
        {"item": "CREATION_ROUTE", "value": str(route_count)},
        {"item": "USED_IN_BREEDING", "value": str(used_count)},
        {"item": "GLOBAL_4BODY_TABLE", "value": str(global4_count)},
        {"item": "UNKNOWN文脈", "value": str(unknown_count)},
        {"item": "4体配合として取得", "value": str(four_body_count)},
        {"item": "4親そろった4体配合", "value": str(valid_four_body_count)},
        {"item": "圧縮表記から正規化した4体配合", "value": str(compact_four_body_count)},
        {"item": "4体構成一致・順序/組み分け相違候補", "value": str(four_body_order_conflict_count)},
        {"item": "左セルを子として取得", "value": str(child_left_count)},
        {"item": "入手方法混入疑い", "value": str(acquisition_like)},
        {"item": "一般ルールに包含される具体例候補", "value": str(len(containment_rows))},
        {"item": "No.128 サーベルきつね代表例", "value": "OK" if saber_ok else "NG"},
    ]
    write_csv(OUT / "altema_breeding_summary.csv", summary, ["item","value"])

    print("\n===== DONE =====")
    for r in summary:
        print(f"{r['item']}: {r['value']}")
    print(f"\n出力先: {OUT}")
    print("まず altema_breeding_summary.csv と altema_breeding_diff_vs_current.csv を確認してください。")


if __name__ == "__main__":
    main()
