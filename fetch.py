#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取公开 IPTV 源 → 归一化命名 → 归类 → 去重 → 探活 → 输出 live.m3u / live.txt

GitHub Actions 每天跑一次。只用标准库。
"""
import re
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TIMEOUT = 8
WORKERS = 24

SRC_TXT = "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.txt"
SRC_M3U = "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"

GROUP_MAP = {
    "央视频道": "央视",
    "卫视频道": "卫视",
    "数字频道": "其他",
    "儿童频道": "其他",
    "纪录频道": "其他",
    "音乐频道": "其他",
    # 电影频道(点播)、春晚频道(回放)、直播中国(景区)、更新时间、地方频道 都不要
}

DROP_NAME = re.compile(
    r"支持作者|更新时间|www\.|^https?://|^央视$|^卫视$|^其他$|^江西$"
    r"|中央电视塔|^\d{4}-\d{2}-\d{2}|^\d{4}年春晚"
)


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
    for enc in ("utf-8", "gbk", "utf-8-sig"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_txt(text):
    out, group = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("#genre#"):
            group = line[:-7].rstrip(",").strip()
            continue
        if line.startswith("#") or "," not in line:
            continue
        name, url = line.split(",", 1)
        if name.strip() and url.strip().startswith("http"):
            out.append((name.strip(), url.strip(), group))
    return out


def parse_m3u(text):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            m = re.search(r",(.+)$", line)
            cur = m.group(1).strip() if m else None
        elif line.startswith("#") or not cur:
            continue
        else:
            out.append((cur, line, None))
            cur = None
    return out


def norm(name):
    """统一命名：CCTV-1 / CCTV 1 / CCTV1 → CCTV1"""
    n = name.strip().rstrip("\r")
    n = re.sub(r"CCTV[\s\-_]*(\d+)(\s*\+)?",
               lambda m: f"CCTV{m.group(1)}" + ("+" if m.group(2) else ""),
               n, flags=re.I)
    n = re.sub(r"\s*[\(\[]\s*(1080|720|576|480|2160)\s*[pi]?\s*[\)\]]", "", n, flags=re.I)
    n = re.sub(r"\s*[\(\[]\s*(geo-blocked|not 24/7|multi|sd|hd|uhd)\s*[\)\]]", "", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", n)
    return n.strip(" ,-")


def classify(name, group):
    # 来源自带分组：只认映射表里的，其余（电影/春晚/直播中国/地方频道等）一律丢弃
    if group is not None:
        return GROUP_MAP.get(group)
    # 没有分组（m3u 源）：按名字判断
    if re.search(r"CCTV|CGTN|中央", name, re.I):
        return "央视"
    if "卫视" in name:
        return "卫视"
    return "其他"


def alive(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def cctv_key(name):
    """CCTV1..17 → 5+ → CGTN → 4K/8K"""
    u = name.upper()
    m = re.match(r"CCTV\s*(\d+)(\+)?", name, re.I)
    if m:
        if m.group(2):
            return 900
        if "4K" in u:
            return 970
        if "8K" in u:
            return 980
        return int(m.group(1))
    if "CGTN" in u:
        return 910
    if "8K" in u:
        return 980
    if "4K" in u:
        return 970
    return 920


def main():
    seen = {}

    def add(name, url, group, src):
        if not name or DROP_NAME.search(name):
            return
        if "geo-blocked" in name.lower():
            return
        name = norm(name)
        if not name:
            return
        target = classify(name, group)
        if not target:
            return
        # iptv-org 只补央视和卫视，其他杂项不要
        if src == 2 and target not in ("央视", "卫视"):
            return
        key = url.split("?")[0].rstrip("/")
        if key in seen:
            return
        seen[key] = (name, url, target, src)

    try:
        items = parse_txt(fetch(SRC_TXT))
        print(f"[OK] vbskycn → {len(items)}")
        for n, u, g in items:
            add(n, u, g, 1)
    except Exception as e:
        print(f"[FAIL] vbskycn → {e}")

    try:
        items = parse_m3u(fetch(SRC_M3U))
        print(f"[OK] iptv-org → {len(items)}")
        for n, u, g in items:
            add(n, u, g, 2)
    except Exception as e:
        print(f"[FAIL] iptv-org → {e}")

    print(f"筛选后 {len(seen)}，探活中…")
    keys = list(seen.keys())
    keep = set()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(alive, seen[k][1]): k for k in keys}
        done = 0
        for f in as_completed(futs):
            done += 1
            if f.result():
                keep.add(futs[f])
            if done % 80 == 0:
                print(f"  {done}/{len(keys)} 可用 {len(keep)}")

    rows = [seen[k] for k in keys if k in keep]
    print(f"可用 {len(rows)}")

    groups = {"央视": [], "卫视": [], "江西": [], "其他": []}
    for name, url, target, src in rows:
        groups[target].append((name, url, src))

    groups["央视"].sort(key=lambda x: (cctv_key(x[0]), x[0], x[2]))
    for g in ("卫视", "江西", "其他"):
        groups[g].sort(key=lambda x: (x[0], x[2]))

    # 江西卫视同时出现在「卫视」和「江西」两个分组
    jx = [r for r in groups["卫视"] if r[0].startswith("江西")]
    if jx and not groups["江西"]:
        groups["江西"] = list(jx)

    order = ["央视", "卫视", "江西", "其他"]
    with open("live.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for g in order:
            for name, url, _ in groups[g]:
                f.write(f'#EXTINF:-1 group-title="{g}",{name}\n{url}\n')

    with open("live.txt", "w", encoding="utf-8") as f:
        for g in order:
            if groups[g]:
                f.write(f"{g},#genre#\n")
                for name, url, _ in groups[g]:
                    f.write(f"{name},{url}\n")

    print("统计:", {g: len(v) for g, v in groups.items()})
    print("央视频道:", sorted({r[0] for r in groups["央视"]}, key=cctv_key))
    print("卫视频道数:", len({r[0] for r in groups["卫视"]}))


if __name__ == "__main__":
    main()
