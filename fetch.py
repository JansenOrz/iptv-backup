#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取公开 IPTV 源 → 归类 → 去重 → 探活 → 输出 live.m3u / live.txt

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

# 源1：分组标记清晰的国内 txt
SRC_TXT = "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.txt"
# 源2：iptv-org 的中国列表（m3u）
SRC_M3U = "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"

GROUP_MAP = {
    "央视频道": "央视",
    "卫视频道": "卫视",
    "数字频道": "其他",
    "儿童频道": "其他",
    "纪录频道": "其他",
    "体育频道": "其他",
    "音乐频道": "其他",
    "春晚频道": "其他",
    # 电影频道（多为点播片）、直播中国（景区）、更新时间 一律不要
}

DROP_NAME = re.compile(r"支持作者|更新时间|www\.|^https?://|^央视$|^卫视$|^其他$|春晚|^\d{4}$")


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
        name, url = name.strip(), url.strip()
        if name and url.startswith("http"):
            out.append((name, url, group))
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


def clean_name(name):
    """去掉 (1080p)、(720p)、[Not 24/7] 之类的后缀"""
    n = re.sub(r"\s*[\(\[][^\)\]]*(p|i|24/7|Geo-blocked|Geo|multi|SD|HD)[^\)\]]*[\)\]]",
               "", name, flags=re.I)
    return n.strip()


def classify(name, group):
    if group in GROUP_MAP:
        return GROUP_MAP[group]
    if group == "地方频道":
        return "江西" if "江西" in name else None   # 地方小台噪声大，不要
    if re.search(r"CCTV|CGTN|中央", name, re.I):
        return "央视"
    if "卫视" in name:
        return "卫视"
    if "江西" in name:
        return "江西"
    return "其他"


def alive(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def cctv_key(name):
    if re.search(r"(4K|8K)", name, re.I):
        return 950
    if re.search(r"5\s*\+", name):
        return 900
    m = re.search(r"CCTV\s*-?\s*(\d+)", name, re.I)
    return int(m.group(1)) if m else 800


def collect():
    seen = {}

    def add(name, url, group, src):
        if not name or DROP_NAME.search(name):
            return
        if "geo-blocked" in name.lower():
            return
        name = clean_name(name)
        if not name:
            return
        target = classify(name, group)
        if not target:
            return
        # iptv-org 只贡献央视和卫视，其余杂项不要
        if src == 2 and target not in ("央视", "卫视"):
            return
        key = url.split("?")[0]
        if key in seen:
            return
        seen[key] = (name, url, target, src)

    try:
        items = parse_txt(fetch(SRC_TXT))
        print(f"[OK] vbskycn → {len(items)} 条")
        for n, u, g in items:
            add(n, u, g, 1)
    except Exception as e:
        print(f"[FAIL] vbskycn → {e}")

    try:
        items = parse_m3u(fetch(SRC_M3U))
        print(f"[OK] iptv-org → {len(items)} 条")
        for n, u, g in items:
            add(n, u, g, 2)
    except Exception as e:
        print(f"[FAIL] iptv-org → {e}")

    return seen


def main():
    seen = collect()
    print(f"筛选后 {len(seen)} 条，开始探活…")

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
                print(f"  探测 {done}/{len(keys)} 可用 {len(keep)}")

    rows = [seen[k] for k in keys if k in keep]
    print(f"可用 {len(rows)} 条")

    groups = {"央视": [], "卫视": [], "江西": [], "其他": []}
    for name, url, target, src in rows:
        groups[target].append((name, url, src))
    # 央视按编号；其余按名称；同名的国内源排前面
    groups["央视"].sort(key=lambda x: (cctv_key(x[0]), x[2], x[0]))
    for g in ("卫视", "江西", "其他"):
        groups[g].sort(key=lambda x: (x[0], x[2]))

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


if __name__ == "__main__":
    main()
