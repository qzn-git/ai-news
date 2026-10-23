#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 日报构建脚本（纯标准库，无需任何第三方依赖）

功能：
  1. 抓取 config.json 中配置的 RSS / Atom 源
  2. 解析条目（标题、链接、摘要、发布时间、来源）
  3. 按关键词把每条新闻归类到：大模型 / 智能体 / 具身智能 / 综合
  4. 去重、按时间排序、按 days_back 过滤
  5. 输出 data/news.json（供前端 index.html 加载）

用法：
  python build.py                      # 使用 config.json
  python build.py --config xxx.json    # 指定配置
  python build.py --out data/news.json # 指定输出
  python build.py --no-archive         # 不写归档快照
"""

import argparse
import hashlib
import json
import os
import re
import sys
import html
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))

# 分类优先级（命中数相同时靠前者胜出）
CATEGORY_PRIORITY = ["llm", "agent", "embodied", "general"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_feed(url, timeout=20):
    """抓取 feed 内容，支持 http(s) 链接与本地文件路径。"""
    if url.startswith("http://") or url.startswith("https://"):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AINewsBot/1.0; +https://github.com/)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")
    else:
        # 本地文件（用于本地验证 / 测试）
        with open(url, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    # 尝试 RFC 2822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # 尝试 ISO 8601 (Atom)
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None


def localname(tag):
    """从 {ns}local 中取出 local。"""
    return tag.split("}")[-1] if "}" in tag else tag


def parse_feed(raw, source_name):
    """解析 RSS 2.0 或 Atom，返回条目列表。"""
    items = []
    try:
        # 清洗 XML 1.0 不允许的控制字符，兼容个别源（如雷峰网）
        raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"  ! 解析失败 [{source_name}]: {e}")
        return items

    root_tag = localname(root.tag).lower()

    if root_tag in ("rss", "feed") or True:
        # 收集所有 item / entry
        found = []
        for el in root.iter():
            ln = localname(el.tag).lower()
            if ln in ("item", "entry"):
                found.append(el)

        for entry in found:
            title = summary = link = guid = pub = ""
            for child in entry:
                ln = localname(child.tag).lower()
                if ln == "title":
                    title = (child.text or "").strip()
                elif ln in ("description", "summary", "content", "encoded"):
                    if not summary:
                        summary = strip_html(child.text or "")
                elif ln == "link":
                    # RSS: <link>text</link> ; Atom: <link href="..">
                    if child.text and child.text.strip():
                        link = child.text.strip()
                    elif child.get("href"):
                        link = child.get("href").strip()
                elif ln in ("pubdate", "published", "updated", "date"):
                    if not pub:
                        pub = child.text or ""
                elif ln in ("guid", "id"):
                    guid = (child.text or "").strip()

            if not link and guid:
                link = guid
            if not title:
                continue

            dt = parse_date(pub)
            items.append(
                {
                    "title": strip_html(title)[:200],
                    "link": link,
                    "summary": summary[:400],
                    "source": source_name,
                    "published": dt.isoformat() if dt else None,
                    "published_ts": dt.timestamp() if dt else 0.0,
                }
            )
    return items


def classify(text, categories):
    """返回分类 key；按关键词命中数打分，命中数为 0 归入 general。"""
    low = text.lower()
    best = "general"
    best_score = 0
    for key in CATEGORY_PRIORITY:
        if key == "general":
            continue
        kws = categories.get(key, {}).get("keywords", [])
        score = sum(low.count(str(k).lower()) for k in kws)
        if score > best_score:
            best_score = score
            best = key
    return best


# ---------------- 中文翻译 ----------------

TRANSLATE_PROVIDERS = ["google", "mymemory"]

TRANSLATE_URLS = {
    "google": "https://translate.googleapis.com/translate_a/single",
    "mymemory": "https://api.mymemory.translated.net/get",
}


def cjk_ratio(text):
    """中文字符占全部字母类字符的比例。"""
    if not text:
        return 1.0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
    return cjk / letters if letters else 1.0


def _translate_google(text, target):
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}
    )
    req = urllib.request.Request(
        TRANSLATE_URLS["google"] + "?" + params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AINewsBot/1.0)"},
    )
    delays = [2, 5, 10, 20]  # 429 退避重试
    for attempt in range(len(delays) + 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = "".join(seg[0] for seg in data[0] if seg and seg[0])
            return out.strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            raise


def _translate_mymemory(text, target):
    # mymemory 单次上限 500 字符
    text = text[:500]
    langpair = "en|zh-CN" if target == "zh-CN" else f"en|{target}"
    params = urllib.parse.urlencode({"q": text, "langpair": langpair})
    req = urllib.request.Request(
        TRANSLATE_URLS["mymemory"] + "?" + params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AINewsBot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("responseData") or {}).get("translatedText", "").strip()


PROVIDER_FAIL_LIMIT = 3
_provider_fails = {}  # provider -> 连续失败次数（会话内 fail-fast）


def translate_text(text, cache, target="zh-CN", aggressive=False):
    """依次尝试多个免费翻译接口（无需 key），带缓存；失败返回空串。"""
    if not text:
        return ""
    if cjk_ratio(text) >= 0.4:
        return text  # 已是中文
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if key in cache:
        return cache[key]
    out = ""
    for provider in TRANSLATE_PROVIDERS:
        if not aggressive and _provider_fails.get(provider, 0) >= PROVIDER_FAIL_LIMIT:
            continue  # 本轮该接口已连续失败多次，跳过
        try:
            if provider == "google":
                out = _translate_google(text, target)
            elif provider == "mymemory":
                out = _translate_mymemory(text, target)
            if out and cjk_ratio(out) >= 0.15:
                _provider_fails[provider] = 0
                break
            out = ""
        except Exception as e:
            _provider_fails[provider] = _provider_fails.get(provider, 0) + 1
            log(f"  ! [{provider}] 翻译失败({_provider_fails[provider]}): {repr(e)[:60]}")
    if out:
        cache[key] = out
        return out
    return ""  # 失败：交给调用方处理（不留英文）


def translate_items(items, cache_path):
    """把条目的 title/summary 翻译成中文（带持久缓存）。"""
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    # 第一轮：标题优先（配额紧张时先保住标题），再摘要
    for e in items:
        e["title"] = translate_text(e["title"], cache) or e["title"]
        time.sleep(0.4)
    for e in items:
        if e.get("summary"):
            e["summary"] = translate_text(e["summary"], cache) or e["summary"]
            time.sleep(0.4)

    # 第二轮（扫尾）：对仍未中文化的标题/摘要强制重试，忽略 fail-fast 限制
    pending = [e for e in items
               if cjk_ratio(e["title"]) < 0.4
               or (e.get("summary") and cjk_ratio(e["summary"]) < 0.4)]
    if pending:
        log(f"扫尾重试 {len(pending)} 条未中文化内容...")
        time.sleep(5)
        for e in pending:
            if cjk_ratio(e["title"]) < 0.4:
                t = translate_text(e["title"], cache, aggressive=True)
                if t:
                    e["title"] = t
                time.sleep(0.6)
            if e.get("summary") and cjk_ratio(e["summary"]) < 0.4:
                s = translate_text(e["summary"], cache, aggressive=True)
                if s:
                    e["summary"] = s
                time.sleep(0.6)

    # 清理：标题未中文化的条目整条移除；摘要未中文化的清空摘要
    dropped = 0
    for e in items:
        if cjk_ratio(e["title"]) < 0.4:
            e["_keep"] = False
            dropped += 1
        elif e.get("summary") and cjk_ratio(e["summary"]) < 0.4:
            e["summary"] = ""
    if dropped:
        log(f"移除 {dropped} 条标题未翻译的条目（明天自动重试）")

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        log(f"翻译完成：缓存 {len(cache)} 段 -> {cache_path}")
    except Exception as e:
        log(f"  ! 缓存写出失败: {repr(e)[:80]}")


def build(config, out_path, archive=True):
    site = config.get("site", {})
    categories = config.get("categories", {})
    feeds = config.get("feeds", [])
    max_per = int(site.get("max_items_per_category", 40))
    days_back = int(site.get("days_back", 21))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days_back)

    # key -> {label, color, items:[]}
    buckets = {k: {"label": v.get("label", k), "color": v.get("color", "#64748b"), "items": []}
               for k, v in categories.items()}
    # 仅保留配置里出现的分类（含 general）
    buckets = {k: buckets[k] for k in categories if k in buckets}

    seen = set()
    total = 0
    errors = []

    for feed in feeds:
        name = feed.get("name", feed.get("url"))
        url = feed.get("url")
        log(f"抓取: {name}  ({url})")
        try:
            raw = fetch_feed(url)
        except Exception as e:
            errors.append(f"{name}: {repr(e)[:120]}")
            log(f"  ! 抓取失败: {repr(e)[:120]}")
            continue
        entries = parse_feed(raw, name)
        log(f"  -> 解析到 {len(entries)} 条")
        for e in entries:
            # 去重
            dedup_key = (e["link"] or e["title"]).strip().lower()
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            # 时间过滤
            if e["published_ts"] and e["published_ts"] < cutoff.timestamp():
                continue
            cat = classify(e["title"] + " " + e["summary"], categories)
            e["category"] = cat
            buckets[cat]["items"].append(e)
            total += 1

    # 每个分类按时间倒序 + 截断
    kept = []
    for k, b in buckets.items():
        b["items"].sort(key=lambda x: x["published_ts"], reverse=True)
        b["items"] = b["items"][:max_per]
        b["count"] = len(b["items"])
        kept.extend(b["items"])

    # 翻译成中文（仅最终保留的条目，带缓存）
    if site.get("translate_to_zh", True):
        cache_path = os.path.join(os.path.dirname(out_path), "translate_cache.json")
        log(f"开始翻译 {len(kept)} 条（标题+摘要）...")
        translate_items(kept, cache_path)
        # 移除标题未翻译成功的条目，保证站点 100% 中文
        for k, b in buckets.items():
            before = len(b["items"])
            b["items"] = [i for i in b["items"] if i.get("_keep", True)]
            b["count"] = len(b["items"])
            if before != b["count"]:
                log(f"  分类 {b['label']}：移除 {before - b['count']} 条未翻译条目")
        for i in kept:
            i.pop("_keep", None)

    output = {
        "title": site.get("title", "AI 日报"),
        "subtitle": site.get("subtitle", ""),
        "description": site.get("description", ""),
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "total": total,
        "source_count": len(feeds),
        "error_count": len(errors),
        "errors": errors,
        "categories": [
            {
                "key": k,
                "label": b["label"],
                "color": b["color"],
                "count": b["count"],
                "items": b["items"],
            }
            for k, b in buckets.items()
        ],
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"已写出 {out_path}（共 {total} 条，来源 {len(feeds)} 个，失败 {len(errors)} 个）")

    if archive:
        arch_dir = os.path.join(os.path.dirname(out_path), "archive")
        os.makedirs(arch_dir, exist_ok=True)
        arch_path = os.path.join(arch_dir, now.strftime("%Y-%m-%d") + ".json")
        with open(arch_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        log(f"已归档 {arch_path}")

    return output


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "news.json"))
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        log(f"配置文件不存在: {args.config}")
        sys.exit(1)

    config = load_config(args.config)
    build(config, args.out, archive=not args.no_archive)


if __name__ == "__main__":
    main()
