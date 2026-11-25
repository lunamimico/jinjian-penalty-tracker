#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金融监管处罚自动采集脚本（免服务器 / 零第三方依赖，仅使用 Python 标准库）
聚焦范围：票据、个人经营贷、贷款“三查”
运行方式：python collector.py
输出文件：penalties-data.json（与历史数据自动去重合并，抓取异常时保留历史数据不清空）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

DATA_FILE = Path(__file__).resolve().parent / "penalties-data.json"
HTML_FILE = Path(__file__).resolve().parent / "index.html"
CACHE_FILE = Path(__file__).resolve().parent / ".collector_cache.json"
CN_TZ = timezone(timedelta(hours=8))

# 专项关键词规则（命中任一专项即保留）
TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "票据": [
        "票据", "银行承兑汇票", "商业承兑汇票", "承兑汇票",
        "贴现", "转贴现", "无真实贸易背景", "票据保证金", "贸易背景审查", "贸易背景审核"
    ],
    "个人经营贷": [
        "个人经营贷", "个人经营性贷款", "个人经营性", "经营性贷款", "无实际经营主体"
    ],
    "三查": [
        "三查", "贷前调查", "贷时审查", "贷中审查", "贷后管理", "贷后检查",
        "授信后管理", "资金监控不到位", "资金支付管理不尽职", "信贷资金被挪用", "贷款资金被挪用"
    ],
}

# 公开信息源（4113=总局机关，4114=监管局，4115=监管分局）
NFRA_LIST_TEMPLATE = (
    "https://www.nfra.gov.cn/cbircweb/DocInfo/SelectDocByItemIdAndChild"
    "?itemId={item_id}&pageSize={page_size}&pageIndex={page_index}"
)
NFRA_DETAIL_ENDPOINT = "https://www.nfra.gov.cn/cbircweb/DocInfo/SelectByDocId?docId={doc_id}"
DEFAULT_ITEMS = ("4113", "4114", "4115")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.nfra.gov.cn/cn/view/pages/ItemList.html?itemPId=923&itemId=4113&itemUrl=ItemListRightList.html&itemName=%E8%A1%8C%E6%94%BF%E5%A4%84%E7%BD%9A",
}


class TextExtractor(HTMLParser):
    """将 HTML 转为纯文本"""

    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "tr", "div", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        return re.sub(r"\s+", " ", raw).strip()


def http_get(url: str, timeout: int = 18, retries: int = 3) -> Optional[str]:
    ctx = ssl.create_default_context()
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="ignore")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 502, 503) and attempt < retries - 1:
                wait = 3.0 * (attempt + 1)
                print(f"[WAIT] HTTP {exc.code}，{wait:.0f}s 后重试...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"[WARN] 请求失败 {url}: {exc}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            print(f"[WARN] 请求失败 {url}: {exc}", file=sys.stderr)
            return None
    return None


def classify_topics(text: str) -> Tuple[List[str], List[str]]:
    categories: List[str] = []
    matched: List[str] = []
    for topic, kws in TOPIC_KEYWORDS.items():
        hits = [kw for kw in kws if kw in text]
        if hits:
            categories.append(topic)
            matched.extend(hits[:3])
    return categories, list(dict.fromkeys(matched))


def infer_institution_type(name: str) -> str:
    big_banks = ("工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行", "邮政储蓄银行")
    joint_banks = (
        "招商银行", "浦发银行", "上海浦东发展银行", "中信银行", "光大银行",
        "华夏银行", "民生银行", "广发银行", "兴业银行", "平安银行", "浙商银行", "恒丰银行", "渤海银行"
    )
    if any(b in name for b in big_banks):
        return "国有大行"
    if any(b in name for b in joint_banks):
        return "股份制银行"
    if any(k in name for k in ("农村商业银行", "农商银行", "农商行", "农村信用", "农信社", "村镇银行")):
        return "农商行/农信社"
    if "银行" in name:
        return "城商行"
    return "其他金融机构"


class TableParser(HTMLParser):
    """解析总局行政处罚公示页 docClob 中的 HTML 表格"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._skip = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", "", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if not self._skip and self._cell is not None:
            self._cell.append(data)


def parse_fine_amount(text: str) -> Tuple[float, str]:
    # 累加文本中出现的所有机构罚款金额，如 "罚款160万元"、"合计罚款380万元"、"罚没款合计611.87万元"
    m_total = re.search(r"合计[^\d]{0,8}(\d+(?:\.\d+)?)\s*万元", text)
    if m_total:
        val = round(float(m_total.group(1)), 2)
        return val, f"{val:g}万元"
    m_wan = re.search(r"罚(?:没款|款)[^\d]{0,10}(\d+(?:\.\d+)?)\s*万元", text)
    if m_wan:
        val = round(float(m_wan.group(1)), 2)
        return val, f"{val:g}万元"
    m_yuan = re.search(r"罚(?:没款|款)[^\d]{0,10}(\d+(?:\.\d+)?)\s*元", text)
    if m_yuan:
        val = round(float(m_yuan.group(1)) / 10000.0, 2)
        return val, f"{val:g}万元"
    return 0.0, "详见决定书"


def extract_doc_no(text: str, fallback: str = "行政处罚公开表") -> str:
    m = re.search(r"[\u4e00-\u9fa5]{1,10}罚决字[〔\[\(（]\d{4}[〕\]\)）]\d+(?:[-—至]\d+)?号", text)
    return m.group(0) if m else fallback


def build_compliance_note(categories: List[str]) -> str:
    notes = []
    if "票据" in categories:
        notes.append("严格核查票据真实贸易背景、增值税发票及保证金来源，杜绝空转开票。")
    if "个人经营贷" in categories:
        notes.append("穿透核验个人经营贷实际经营实体、流水匹配度及资金闭环流向。")
    if "三查" in categories:
        notes.append("压实贷前实地调查、贷时独立审查与贷后资金用途跟踪检查责任。")
    return "".join(notes) or "建议对照监管处罚事由开展同类业务合规自查。"


def make_record_id(doc_no: str, institution: str, date_str: str) -> str:
    seed = f"{doc_no}|{institution}|{date_str}".encode("utf-8")
    return "nfra-" + hashlib.sha1(seed).hexdigest()[:12]


def infer_region(authority: str) -> str:
    provinces = (
        "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
        "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
        "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "内蒙古",
        "广西", "西藏", "宁夏", "新疆", "深圳", "大连", "宁波", "厦门", "青岛",
    )
    for p in provinces:
        if p in authority:
            return p
    if "总局" in authority:
        return "全国"
    return authority[:2] if len(authority) >= 2 else "全国"


def split_institution_and_responsible(party_cell: str, penalty_cell: str) -> Tuple[str, str]:
    party = party_cell.strip()
    responsible = "详见决定书"
    # 兼容 "及相关责任人员" / "及相关责任人" / "等责任人员"
    cleaned = re.sub(r"[及等]相关责任人员?$", "", party).strip("、，, ")
    if cleaned != party:
        party = cleaned
        m_resp = re.search(r"(对[^。；;]{1,80}(?:警告|禁止|取消|罚款)[^。；;]{0,40})", penalty_cell)
        if m_resp:
            responsible = m_resp.group(1)
    return party or party_cell, responsible


def is_institution_party(party: str) -> bool:
    """仅保留银行/金融机构为主体的罚单，过滤单独成行的个人责任人记录"""
    if not party or len(party) < 4:
        return False
    if re.search(r"[（(]时任|原[^）)]*员工", party):
        return False
    return any(k in party for k in ("银行", "信用", "金融", "农商", "村镇", "信托", "财务公司", "分行", "支行", "联社"))


def parse_detail_table(
    doc_id: Any,
    doc_title: str,
    pub_date: str,
    doc_source: str,
    doc_clob: str,
) -> List[Dict[str, Any]]:
    parser = TableParser()
    parser.feed(doc_clob or "")
    rows = parser.rows
    if not rows:
        return []

    records: List[Dict[str, Any]] = []
    detail_url = f"https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId={doc_id}"

    # 定位表头列索引（兼容 5 列「序号/当事人名称/主要违法违规行为/行政处罚内容/作出决定机关」和旧版键值表）
    header_idx = -1
    col_map: Dict[str, int] = {}
    for idx, r in enumerate(rows[:5]):
        joined = "".join(r)
        if "主要违法违规" in joined and ("处罚" in joined or "当事人" in joined):
            header_idx = idx
            for ci, cell in enumerate(r):
                if "当事人" in cell or "名称" in cell:
                    col_map["party"] = ci
                elif "违法违规" in cell or "案由" in cell or "事实" in cell:
                    col_map["violation"] = ci
                elif "处罚内容" in cell or "处罚决定" in cell:
                    col_map["penalty"] = ci
                elif "决定机关" in cell or "机关" in cell:
                    col_map["authority"] = ci
                elif "文号" in cell:
                    col_map["docNo"] = ci
            break

    if header_idx >= 0 and "violation" in col_map:
        for r in rows[header_idx + 1 :]:
            if len(r) <= col_map["violation"]:
                continue
            party_raw = r[col_map["party"]] if "party" in col_map and col_map["party"] < len(r) else ""
            violation = r[col_map["violation"]].strip()
            penalty = r[col_map["penalty"]].strip() if "penalty" in col_map and col_map["penalty"] < len(r) else ""
            authority = (
                r[col_map["authority"]].strip()
                if "authority" in col_map and col_map["authority"] < len(r) and r[col_map["authority"]].strip()
                else (doc_source or "国家金融监督管理总局")
            )
            doc_no_raw = r[col_map["docNo"]].strip() if "docNo" in col_map and col_map["docNo"] < len(r) else ""

            if not violation or not party_raw:
                continue

            combined = f"{party_raw} {violation} {penalty}"
            categories, matched = classify_topics(combined)
            if not categories:
                continue

            institution, responsible = split_institution_and_responsible(party_raw, penalty)
            if not is_institution_party(institution):
                continue
            fine_amount, fine_display = parse_fine_amount(penalty)
            doc_no = extract_doc_no(doc_no_raw or penalty or doc_title, fallback=f"{authority[:6]}公开表({pub_date})")

            records.append(
                {
                    "id": make_record_id(doc_no, institution, pub_date),
                    "docNo": doc_no,
                    "institution": institution,
                    "institutionType": infer_institution_type(institution),
                    "fineAmount": fine_amount,
                    "fineDisplay": fine_display,
                    "penaltyDetail": penalty or fine_display,
                    "responsible": responsible,
                    "authority": authority,
                    "region": infer_region(authority),
                    "date": pub_date or datetime.now(CN_TZ).strftime("%Y-%m-%d"),
                    "categories": categories,
                    "matchedKeywords": matched,
                    "violation": violation,
                    "complianceNote": build_compliance_note(categories),
                    "sourceUrl": detail_url,
                }
            )
    else:
        # 兼容单条处罚的键值对表格（左侧字段名、右侧字段值）
        kv: Dict[str, str] = {}
        for r in rows:
            if len(r) >= 2:
                kv[r[0]] = r[1]
        violation = next((v for k, v in kv.items() if "违法违规" in k or "案由" in k), "")
        party_raw = next((v for k, v in kv.items() if "当事人" in k or "名称" in k), "")
        penalty = next((v for k, v in kv.items() if "处罚决定" in k or "处罚内容" in k), "")
        authority = next((v for k, v in kv.items() if "机关" in k), "") or doc_source or "国家金融监督管理总局"
        doc_no_raw = next((v for k, v in kv.items() if "文号" in k), "")

        combined = f"{party_raw} {violation} {penalty}"
        categories, matched = classify_topics(combined)
        if categories and violation and party_raw:
            institution, responsible = split_institution_and_responsible(party_raw, penalty)
            if is_institution_party(institution):
                fine_amount, fine_display = parse_fine_amount(penalty)
                doc_no = extract_doc_no(doc_no_raw or doc_title, fallback=f"{authority[:6]}公开表({pub_date})")
                records.append(
                    {
                        "id": make_record_id(doc_no, institution, pub_date),
                        "docNo": doc_no,
                        "institution": institution,
                        "institutionType": infer_institution_type(institution),
                        "fineAmount": fine_amount,
                        "fineDisplay": fine_display,
                        "penaltyDetail": penalty or fine_display,
                        "responsible": responsible,
                        "authority": authority,
                        "region": infer_region(authority),
                        "date": pub_date or datetime.now(CN_TZ).strftime("%Y-%m-%d"),
                        "categories": categories,
                        "matchedKeywords": matched,
                        "violation": violation,
                        "complianceNote": build_compliance_note(categories),
                        "sourceUrl": detail_url,
                    }
                )

    return records


def load_seen_doc_ids() -> Set[int]:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(int(x) for x in data)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return set()


def save_seen_doc_ids(seen: Set[int]) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError:
        pass


def fetch_nfra_records(
    since_date: str = "",
    items: Tuple[str, ...] = DEFAULT_ITEMS,
    start_page: int = 1,
    max_pages: int = 1,
    step: int = 1,
    page_size: int = 30,
    delay: float = 0.6,
    max_docs: int = 0,
    on_batch: Any = None,
) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    seen_doc_ids: Set[int] = load_seen_doc_ids()
    fetched_docs = 0
    step = max(1, step)

    for item_id in items:
        for page_offset in range(max_pages):
            page_index = start_page + page_offset * step
            api_url = NFRA_LIST_TEMPLATE.format(
                item_id=item_id, page_size=page_size, page_index=page_index
            )
            raw = http_get(api_url)
            if not raw:
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                break

            rows = (
                payload.get("data", {}).get("rows")
                if isinstance(payload.get("data"), dict)
                else None
            )
            if not isinstance(rows, list) or not rows:
                break

            first_d = str(rows[0].get("publishDate") or "")[:10]
            last_d = str(rows[-1].get("publishDate") or "")[:10]
            print(f"[PAGE] item={item_id} page={page_index} ({last_d} ~ {first_d})", flush=True)

            reached_since = False
            page_records: List[Dict[str, Any]] = []
            for row in rows:
                pub_date = str(row.get("publishDate") or "")[:10]
                if since_date and pub_date and pub_date < since_date:
                    reached_since = True
                    continue

                doc_id = row.get("docId")
                if not doc_id:
                    continue
                doc_id_int = int(doc_id)
                if doc_id_int in seen_doc_ids:
                    continue

                if max_docs > 0 and fetched_docs >= max_docs:
                    if page_records and on_batch:
                        on_batch(page_records)
                    save_seen_doc_ids(seen_doc_ids)
                    return collected

                time.sleep(delay)
                detail_raw = http_get(NFRA_DETAIL_ENDPOINT.format(doc_id=doc_id_int), timeout=18)
                fetched_docs += 1
                if not detail_raw:
                    continue
                try:
                    detail_payload = json.loads(detail_raw)
                except json.JSONDecodeError:
                    continue

                seen_doc_ids.add(doc_id_int)
                d = detail_payload.get("data") or {}
                doc_clob = str(d.get("docClob") or "")
                if not doc_clob:
                    continue

                doc_title = str(d.get("docSubtitle") or d.get("docTitle") or row.get("docSubtitle") or "").strip()
                doc_source = str(d.get("docSource") or row.get("docSource") or "").strip()
                parsed = parse_detail_table(doc_id_int, doc_title, pub_date, doc_source, doc_clob)
                if parsed:
                    collected.extend(parsed)
                    page_records.extend(parsed)

            if page_records and on_batch:
                on_batch(page_records)
            save_seen_doc_ids(seen_doc_ids)

            if reached_since:
                break
            time.sleep(delay)

    return collected


def load_existing() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "schemaVersion": "1.0",
        "updatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "source": "国家金融监督管理总局及各地监管局公开行政处罚信息公示表",
        "topics": ["票据", "个人经营贷", "三查"],
        "records": [],
    }


def merge_records(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    limit: int = 5000,
) -> Tuple[List[Dict[str, Any]], int]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in existing:
        key = item.get("id") or make_record_id(
            str(item.get("docNo", "")),
            str(item.get("institution", "")),
            str(item.get("date", "")),
        )
        by_key[key] = item

    added = 0
    for item in incoming:
        key = item["id"]
        if key not in by_key:
            by_key[key] = item
            added += 1

    merged = list(by_key.values())
    merged.sort(key=lambda r: (str(r.get("date", "")), float(r.get("fineAmount", 0) or 0)), reverse=True)
    return merged[:limit], added


def save_dataset(dataset: Dict[str, Any]) -> None:
    dataset["updatedAt"] = datetime.now(CN_TZ).isoformat(timespec="seconds")
    DATA_FILE.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sync_to_html(dataset: Dict[str, Any]) -> None:
    if not HTML_FILE.exists():
        return
    try:
        html = HTML_FILE.read_text(encoding="utf-8")
        indented = "\n".join(
            ("      " + line if i > 0 else line)
            for i, line in enumerate(json.dumps(dataset, ensure_ascii=False, indent=2).splitlines())
        )
        new_html, count = re.subn(
            r"const SEED_DATASET = \{.*?\n      \};",
            lambda _: "const SEED_DATASET = " + indented + ";",
            html,
            count=1,
            flags=re.S,
        )
        if count == 1:
            HTML_FILE.write_text(new_html, encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="金融监管处罚专项自动采集（票据/个人经营贷/三查）")
    parser.add_argument("--since", default="", help="起始日期 YYYY-MM-DD（如 2024-01-01），留空为每日增量模式")
    parser.add_argument("--items", default="4113,4114,4115", help="栏目 ID，逗号分隔（4113总局,4114监管局,4115分局）")
    parser.add_argument("--start-page", type=int, default=1, help="起始页码（可用于直接跳到 2024/2025 年）")
    parser.add_argument("--max-pages", type=int, default=1, help="每个栏目最多抓取页数")
    parser.add_argument("--step", type=int, default=1, help="跳页步长（>1 时按固定间隔均匀采样各月份）")
    parser.add_argument("--page-size", type=int, default=30, help="每页条数")
    parser.add_argument("--delay", type=float, default=0.6, help="每次请求间隔秒数")
    parser.add_argument("--max-docs", type=int, default=0, help="本次最多抓取详情页数量，0为不限")
    parser.add_argument("--sync-html", action="store_true", help="抓取后同步更新 index.html 内置数据")
    args = parser.parse_args()

    items = tuple(x.strip() for x in args.items.split(",") if x.strip())
    dataset = load_existing()
    total_added = 0

    def handle_batch(batch: List[Dict[str, Any]]) -> None:
        nonlocal total_added
        merged, added = merge_records(dataset.get("records", []), batch)
        dataset["records"] = merged
        if added > 0:
            total_added += added
            save_dataset(dataset)
            if args.sync_html:
                sync_to_html(dataset)
            print(f"[SAVE] 本批新增 {added} 条，当前累计 {len(merged)} 条", flush=True)

    fetch_nfra_records(
        since_date=args.since,
        items=items,
        start_page=args.start_page,
        max_pages=args.max_pages,
        step=args.step,
        page_size=args.page_size,
        delay=args.delay,
        max_docs=args.max_docs,
        on_batch=handle_batch,
    )
    save_dataset(dataset)
    if args.sync_html:
        sync_to_html(dataset)
    print(f"[OK] 采集完成：本次新增 {total_added} 条，当前总计 {len(dataset['records'])} 条，已写入 {DATA_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
