#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TerrySP / Altema 個別モンスターページ 配合再検証ツール v4.1

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
├─ altema_breeding_summary.csv
└─ altema_breeding_raw_sections.csv
"""

from __future__ import annotations

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
        "スライム系モンスター": "スライム",
        "ドラゴン系モンスター": "ドラゴン",
        "魔獣系モンスター": "魔獣",
        "自然系モンスター": "自然",
        "物質系モンスター": "物質",
        "悪魔系モンスター": "悪魔",
        "ゾンビ系モンスター": "ゾンビ",
        "？？？系モンスター": "？？？",
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

        if gt == "rule" and st == "monster" and sno:
            return rule_matches_monster(gname, int(sno), master)

        if gt == "rule" and st == "rule":
            return gname == sname or gname == "相手問わず"

        return False

    return (
        atom_covers(ga[0], sa[0]) and atom_covers(ga[1], sa[1])
    ) or (
        atom_covers(ga[0], sa[1]) and atom_covers(ga[1], sa[0])
    )



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
            return "rule", "", p + "モンスター"

    # 「○○系モンスター」表記
    m = re.search(r"([^\s×|]+系)モンスター", text)
    if m:
        return "rule", "", m.group(1) + "モンスター"

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
        raw_text=raw_text,
    )


def parse_child_recipe_expression(
    child_name: str,
    expr: str,
    name_to_no: dict[str, int],
    source_url: str,
    raw_text: str,
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
    キングモーモン型の4列UIを読む。
    4セルそれぞれに1体ずつ既知モンスターがあり、
    同一tableに「4体配合」表記がある場合のみ採用。
    """
    cells = tr.find_all(["td", "th"], recursive=False)
    if len(cells) != 4:
        return None

    row_names = []
    for c in cells:
        mons = known_monsters_in_cell(c, name_to_no)
        if len(mons) != 1:
            return None
        row_names.append(mons[0])

    if any(n in UI_LABELS_4BODY for n in row_names):
        return None

    table = tr.find_parent("table")
    table_text = normalize_rule_text(node_text(table)) if table else ""
    # UIの4列だけを通常表と誤認しないための必須条件
    if "4体配合" not in table_text:
        return None

    return build_candidate(
        result_no=page_result_no,
        result_name=page_result_name,
        parents=row_names,
        name_to_no=name_to_no,
        source_url=source_url,
        raw_text=" | ".join(row_names),
        recipe_type="4体配合",
        note="4列UIから4体配合を復元",
    )


def parse_pair_from_row(
    tr: Tag,
    result_no: int,
    result_name: str,
    name_to_no: dict[str, int],
    source_url: str,
) -> list[RecipeCandidate]:
    """
    v4:
    1) 4列UIなら4体配合として専用解析
    2) 2列の右セルに「×」があれば、左セルを「子」として解析
    3) それ以外は従来どおり、ページ主役を子として親1/親2を読む
    """
    cells = tr.find_all(["td", "th"], recursive=False)
    if len(cells) < 2:
        return []

    # ---- 4体配合UI ----
    four = parse_four_body_ui_row(
        tr, name_to_no, result_no, result_name, source_url
    )
    if four:
        return [four]

    originals = [normalize_rule_text(node_text(c)) for c in cells]
    texts = [recipe_only_text(x) for x in originals]
    joined = " | ".join(texts)
    original_joined = " | ".join(originals)

    # 見出し/UIラベル行は除外
    if any(x in joined for x in [
        "配合早見表", "への配合パターン",
        "モンスター | 配合・入手方法",
        "親Aへの配合ルート", "親Bへの配合ルート",
    ]):
        return []
    if len(texts) >= 2 and texts[0] in UI_LABELS_4BODY and texts[1] in UI_LABELS_4BODY:
        return []

    # 親セルそのものが入手方法セクションなら除外
    if len(originals) >= 2 and any(
        originals[1].lstrip().startswith(marker) for marker in NON_RECIPE_MARKERS
    ):
        # ただし、同じセルに【特殊配合】等が後続する場合は
        # recipe_only_textでは先頭【入手方法】で切れるので安全側でこの行は使わない。
        return []

    if len(texts) < 2 or not texts[0] or not texts[1]:
        return []

    # ---- 左セル=子 / 右セル=配合式 ----
    # 「モンスター | 配合・入手方法」系の表で最重要。
    left_name = clean_parent_phrase(texts[0])
    right_expr = texts[1]
    if left_name in name_to_no and "×" in right_expr:
        cands = parse_child_recipe_expression(
            left_name, right_expr, name_to_no, source_url, joined
        )
        if cands:
            return cands

        # 4体配合と明記されているが4親へ安全に解けない場合は
        # 誤って2体配合に落とさずREVIEW 1件を残す。
        if "4体配合" in right_expr:
            return [RecipeCandidate(
                result_no=name_to_no[left_name],
                result_name=left_name,
                parent1_type="review", parent1_no="", parent1=right_expr,
                parent2_type="review", parent2_no="", parent2="",
                recipe_type="4体配合",
                confidence="REVIEW",
                note="4体配合表記だが4親へ安全に分解できない",
                source_url=source_url,
                raw_text=joined,
            )]

    # ---- 従来型: ページ主役への2体配合 ----
    def links(c: Tag) -> list[str]:
        return [monster_link_name(a) for a in c.find_all("a", href=True) if monster_link_name(a)]

    p1 = classify_parent_token(texts[0], links(cells[0]), name_to_no)
    p2 = classify_parent_token(texts[1], links(cells[1]), name_to_no)

    if not p1[2] or not p2[2]:
        return []

    # UIラベルは親として保存しない
    if p1[2] in UI_LABELS_4BODY or p2[2] in UI_LABELS_4BODY:
        return []

    # ページ主役自身を親として誤抽出するケースを抑制
    if p1[2] == result_name and ("相手問わず" not in p2[2]):
        return []

    conf = "HIGH"
    note = ""
    if p1[0] == "review" or p2[0] == "review":
        conf = "REVIEW"
        note = "親セル解析が曖昧"

    return [RecipeCandidate(
        result_no=result_no,
        result_name=result_name,
        parent1_type=p1[0], parent1_no=p1[1], parent1=p1[2],
        parent2_type=p2[0], parent2_no=p2[1], parent2=p2[2],
        confidence=conf,
        note=note,
        source_url=source_url,
        raw_text=joined,
    )]


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
                confidence=conf,
                note=note,
                source_url=source_url,
                raw_text=frag,
            ))
    return out


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
        }

    nodes = section_nodes_after_heading(heading)
    sec = section_soup(nodes)
    raw_section = node_text(sec)

    name_to_no = {r["name"]: no for no, r in master.items()}
    # 表記差も別名として登録。
    for no, aliases in NAME_ALIASES.items():
        for a in aliases:
            name_to_no[a] = no

    found: list[RecipeCandidate] = []

    # 1) table行から抽出
    for tr in sec.find_all("tr"):
        cands = parse_pair_from_row(
            tr, expected_no, expected_name, name_to_no, source_url
        )
        found.extend(cands)

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
    # parse_pair_from_row で拾える想定だが、セクション内リンク列でも保険。
    if not found:
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

    found = list(dedup.values())

    return found, {
        "status": "OK" if name_ok and no_ok else "IDENTITY_REVIEW",
        "page_no": page_no or "",
        "title": title,
        "raw_section": raw_section[:3000],
        "reason": "" if name_ok and no_ok else f"name_ok={name_ok}, no_ok={no_ok}",
        "identity_ok": name_ok and no_ok,
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
        ["no","name","review_category","reason","recipe","source_url","raw_text"],
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
        generals = [r for r in arr if any(a[0] == "rule" for a in recipe_parent_atoms(r))]
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
    four_body_count = sum(1 for r in all_rows if r.get("recipe_type") == "4体配合")
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
        {"item": "4体配合として取得", "value": str(four_body_count)},
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
