"""在线汉化查询。
"""

from __future__ import annotations

import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path

from ferrycore.community import CFPA_RELEASE_PAGE, community_pack_translations


def check_online_translation(site: str, modid: str, timeout: float = 6.0) -> tuple[str, str]:
    """探测一个查询源上是否存在某模组的汉化；返回 (found|miss|error, 实际访问的网址)。"""
    url = site.replace("{modid}", modid)
    request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(262144).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return ("miss", url) if exc.code == 404 else ("error", url)
    except OSError:
        return "error", url
    # jsDelivr 对仓库级 404 可能回 200 + 这句提示
    if not body.strip() or "Couldn't find" in body:
        return "miss", url
    return "found", url


def query_online_translations(sites: list[str], modids: list[str], pack_path: Path | None = None, timeout: float = 6.0, progress=None) -> list[dict[str, str]]:
    """并发探测多个模组在多个查询源上的汉化情况；pack_path 给定时先查内置 CFPA 整合包。"""
    usable_sites = [site.strip() for site in sites if "{modid}" in site]
    total = max(1, len(modids) * (len(usable_sites) + (1 if pack_path else 0)))
    done = 0
    results: list[dict[str, str]] = []

    def worker(site: str, modid: str) -> dict[str, str]:
        nonlocal done
        status, url = check_online_translation(site, modid, timeout)
        done += 1
        if progress:
            progress(done, total)
        return {"modid": modid, "site": site, "status": status, "url": url}

    if modids:
        pack_hits = community_pack_translations(pack_path, modids) if pack_path else {}
        for modid in modids:
            done += 1
            if progress:
                progress(done, total)
            results.append({
                "modid": modid,
                "site": "CFPA 社区汉化（内置）",
                "status": "found" if pack_hits.get(modid) else "miss",
                "url": CFPA_RELEASE_PAGE,
            })
        if usable_sites:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(worker, site, modid) for site in usable_sites for modid in modids]
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())
    return results
