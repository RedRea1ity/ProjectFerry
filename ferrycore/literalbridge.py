"""LiteralBridge 第三方桥模组：探测已装版本、下载、自动升级。

── LiteralBridge：按需自动获取 ─────────────────────────────────────────────
LiteralBridge（Losketch 出品）是硬编码文本的「显示层桥」：把模组里写死的字面量
转成可翻译的 key，然后由资源包翻。摆渡只负责生成它的规则文件
config/literalbridge/ferry.json —— 模组本体得先装进实例 mods/ 才有用。

获取策略（2026-10-12 定）：
  网络通           → 直接从 GitHub Release 下匹配「加载器 + MC 版本」的 jar，装进 mods/
  网络不通 / 无匹配 → 回落到打开项目页，让用户自己下

**不为了成功率加第三方 GitHub 代理镜像**（ghproxy 之类）：那等于把一个能换包的
中间人塞进下载链路，而我们只能读 jar 里的 mods.toml，验不了有没有被投毒。
线路不稳就让它不稳 —— 失败时老老实实打开项目页，那条路永远可用。

重要事实：LiteralBridge 目前**只有 Forge 版**，mods.toml 里写的是
  modLoader="javafml" + 依赖 modId="forge" + minecraft versionRange="[1.20.1,1.21)"
所以在 NeoForge / MC 1.21+ 上必然「无匹配」→ 会正确回落到打开项目页，
而不是往 NeoForge 实例里塞一个装不上的 Forge 版 jar。
"""

from __future__ import annotations

import collections
import json
import re
import shutil
import tempfile
import time
import tomllib
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import LOG, redact_secrets
from ferrycore.bridge import LITERALBRIDGE_PAGE
from ferrycore.net import _fetch_json
from ferrycore.update import download_release_asset
from ferrycore.versions import version_key


LITERALBRIDGE_REPO = "Losketch/LiteralBridge"


LITERALBRIDGE_RELEASES_API = f"https://api.github.com/repos/{LITERALBRIDGE_REPO}/releases?per_page=30"


def _mc_version_triple(text: Any) -> tuple[int, int, int]:
    """把 "1.21.10" / "1.21" / "47.3.0" 压成 (major, minor, patch)，缺位补 0。"""
    nums = [int(part) for part in re.findall(r"\d+", str(text))][:3]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def mc_range_allows(mc_range: str, version: str) -> bool:
    """判断版本号 version 是否落在模组声明的 mc_range 区间里。

    只认 Forge / Fabric 最常见的几种写法：
      `[1.20.1,1.21)`  `(1.20,1.21]`  `[1.20.1,)`  `[1.20.1]`  裸版本号
    解析不出来一律放行 —— 宁可放过、由人来看，也不要误拒一个本来能用的版本。
    """
    text = str(mc_range or "").strip()
    if not text or "*" in text:
        return True
    target = _mc_version_triple(version)
    match = re.match(r"^([\[\(])\s*([^,)\]]*?)\s*(?:,\s*([^)\]]*?)\s*)?([\]\)])$", text)
    if not match:
        digits = re.sub(r"[^0-9.]", "", text).strip(".")
        return _mc_version_triple(digits) == target if digits else True
    lower_open, lower_text, upper_text, upper_close = match.groups()
    lower_text = (lower_text or "").strip()
    upper_text = (upper_text or "").strip()
    if lower_text and lower_text not in ("0", "*"):
        low = _mc_version_triple(lower_text)
        if target < low or (target == low and lower_open == "("):
            return False
    if upper_text and upper_text not in ("*",):
        high = _mc_version_triple(upper_text)
        if target > high or (target == high and upper_close == ")"):
            return False
    return True


def _jar_loaders(names: set[str]) -> list[str]:
    """按包内元数据文件名判断这个 jar 支持哪些加载器。"""
    loaders: list[str] = []
    if "META-INF/neoforge.mods.toml" in names:
        loaders.append("neoforge")
    elif "META-INF/mods.toml" in names:
        # 注意：NeoForge 20.4+ 才改用 neoforge.mods.toml，老版 NeoForge 也用 mods.toml，
        # 所以这里判成 forge 可能偏保守 —— 只是用来复核下载结果，错了也只是回落打开网页。
        loaders.append("forge")
    if "fabric.mod.json" in names:
        loaders.append("fabric")
    if "quilt.mod.json" in names:
        loaders.append("quilt")
    return loaders


def read_mod_jar_info(path: Path) -> dict[str, Any]:
    """读 jar 元数据 → {modid, version, loaders, mc_ranges}；读不出来返回 {}。

    纯标准库（zipfile + tomllib + json），不需要 JDK，也不联网。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            loaders = _jar_loaders(names)
            modid = version = ""
            mc_ranges: list[str] = []
            forge_entry = next(
                (entry for entry in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if entry in names),
                "",
            )
            if forge_entry:
                try:
                    data = tomllib.loads(zf.read(forge_entry).decode("utf-8-sig"))
                except (ValueError, UnicodeDecodeError):
                    data = None
                if isinstance(data, dict):
                    mods = data.get("mods")
                    if isinstance(mods, list) and mods and isinstance(mods[0], dict):
                        modid = str(mods[0].get("modId") or "")
                        version = str(mods[0].get("version") or "")
                    dependencies = data.get("dependencies")
                    if isinstance(dependencies, dict):
                        for entries in dependencies.values():
                            if not isinstance(entries, list):
                                continue
                            for dep in entries:
                                if isinstance(dep, dict) and str(dep.get("modId", "")).lower() == "minecraft":
                                    text = str(dep.get("versionRange") or "").strip()
                                    if text:
                                        mc_ranges.append(text)
            if "fabric.mod.json" in names:
                try:
                    data = json.loads(zf.read("fabric.mod.json").decode("utf-8-sig"))
                except (ValueError, UnicodeDecodeError):
                    data = None
                if isinstance(data, dict):
                    modid = modid or str(data.get("id") or "")
                    version = version or str(data.get("version") or "")
                    depends = data.get("depends")
                    if isinstance(depends, dict) and depends.get("minecraft"):
                        mc_ranges.append(str(depends["minecraft"]))
            return {"modid": modid, "version": version, "loaders": loaders, "mc_ranges": mc_ranges}
    except (OSError, zipfile.BadZipFile, KeyError, TypeError):
        LOG.debug("Could not read mod metadata from %s", path, exc_info=True)
        return {}


def detect_mod_loader(instance_path: Path, limit: int = 80) -> str:
    """粗判实例用的加载器（forge / neoforge / fabric / quilt / ""）。

    只看 mods/ 里 jar 的元数据文件名投票：不依赖 launcher 目录结构、不联网。
    投不出来就返回空串，由调用方决定怎么回落。
    """
    mods = Path(instance_path) / "mods"
    if not mods.is_dir():
        return ""
    votes: collections.Counter[str] = collections.Counter()
    for jar in sorted(mods.glob("*.jar"))[:limit]:
        try:
            with zipfile.ZipFile(jar) as zf:
                names = set(zf.namelist())
        except (OSError, zipfile.BadZipFile):
            continue
        if "META-INF/neoforge.mods.toml" in names:
            votes["neoforge"] += 2
        elif "META-INF/mods.toml" in names:
            votes["forge"] += 1
        if "fabric.mod.json" in names:
            votes["fabric"] += 1
        if "quilt.mod.json" in names:
            votes["quilt"] += 1
    return votes.most_common(1)[0][0] if votes else ""


# GitHub 匿名 API 每小时只给 60 次配额，而「译完自动导出」每次都会来问一次更新，
# 所以成功结果缓存 5 分钟。只缓存非空结果 —— 网络失败不能靠缓存记住。
_LB_RELEASE_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


_LB_RELEASE_TTL = 300.0


def literalbridge_releases(timeout: float = 8.0, *, use_cache: bool = True) -> list[dict[str, Any]]:
    """LiteralBridge 的 Release 列表（新的在前），只保留带 .jar 附件的。网络不通返回 []。

    成功结果按 5 分钟 TTL 缓存，避免「每次导出都打一次 GitHub API」把匿名配额刷爆。
    """
    now = time.monotonic()
    if use_cache and _LB_RELEASE_CACHE["data"] is not None and now - _LB_RELEASE_CACHE["at"] < _LB_RELEASE_TTL:
        return list(_LB_RELEASE_CACHE["data"])
    data = _fetch_json(LITERALBRIDGE_RELEASES_API, timeout)
    if not isinstance(data, list):
        return []
    releases: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        assets = [
            {
                "name": str(asset.get("name") or ""),
                "url": str(asset.get("browser_download_url") or ""),
                "size": int(asset.get("size") or 0),
            }
            for asset in (item.get("assets") or [])
            if isinstance(asset, dict)
            and str(asset.get("name") or "").lower().endswith(".jar")
            and asset.get("browser_download_url")
        ]
        if not assets:
            continue
        releases.append({
            "version": str(item.get("tag_name") or item.get("name") or "").lstrip("v"),
            "published": str(item.get("published_at") or "")[:10],
            "page": str(item.get("html_url") or LITERALBRIDGE_PAGE),
            "assets": assets,
        })
    if releases and use_cache:
        _LB_RELEASE_CACHE["data"] = list(releases)
        _LB_RELEASE_CACHE["at"] = now
    return releases


def pick_literalbridge_asset(release: dict[str, Any], loader: str, mc_version: str) -> dict[str, Any] | None:
    """在一个 Release 里挑最可能匹配「加载器 + MC 版本」的附件。

    附件命名不统一（1.2.0 是 `literalbridge-forge-1.20.1-1.2.0.jar`，
    1.3.0 是 `literalbridge-1.3.0.jar`），所以这里只做打分排序；
    选出来的 jar 落盘前还会被 read_mod_jar_info() 复核一次。
    """
    loader = (loader or "").lower()
    mc_version = (mc_version or "").lower()
    best: dict[str, Any] | None = None
    best_score = -1
    for asset in release.get("assets") or []:
        name = asset["name"].lower()
        if any(tag in name for tag in ("-sources", "-javadoc", "-dev", "-api", "-slim")):
            continue
        score = 0
        if loader and loader in name:
            score += 4
        elif loader in ("neoforge", "quilt") and "forge" in name:
            score -= 4          # NeoForge/Quilt 实例别被 forge 版的 jar 骗走
        if mc_version and mc_version in name:
            score += 4
        if re.fullmatch(r"literalbridge[-_]\d[\w.\-]*\.jar", name):
            score += 1          # 通用命名：名字里没带加载器/版本信息
        if score > best_score:
            best, best_score = asset, score
    return best


def _literalbridge_jar_matches(info: dict[str, Any], loader: str, mc_version: str) -> bool:
    """下载后复核：加载器对不对、MC 版本范围包不包含目标版本。"""
    if not info:
        return True             # 读不出元数据就不拦，宁可装上也别误拒
    loaders = [str(item).lower() for item in info.get("loaders") or []]
    if loader and loaders and loader not in loaders:
        return False
    ranges = [str(item).strip() for item in info.get("mc_ranges") or [] if str(item).strip()]
    if mc_version and ranges and not any(mc_range_allows(item, mc_version) for item in ranges):
        return False
    return True


def installed_literalbridge(mods_dir: Path) -> dict[str, Any] | None:
    """找 mods 里已装的 LiteralBridge 本体，返回 {path, name, version}；没装返回 None。

    只认文件名以 `literalbridge` 开头的 `.jar` —— Forge / NeoForge 只加载 `.jar`，
    所以升级时把旧版改名成 `xxx.jar.bak-<时间>` 就不会被重复加载
    （同一个 modId 的两份 jar 同时存在会直接崩游戏，这一步必须做）。
    装了多份时取版本号最大的那份。
    """
    mods = Path(mods_dir)
    if not mods.is_dir():
        return None
    found: dict[str, Any] | None = None
    for jar in sorted(mods.glob("*.jar")):
        if not jar.name.lower().startswith("literalbridge"):
            continue
        info = read_mod_jar_info(jar)
        version = str(info.get("version") or "")
        if not version:
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", jar.name)
            version = match.group(1) if match else ""
        if found is None or version_key(version) > version_key(found["version"]):
            found = {"path": str(jar), "name": jar.name, "version": version}
    return found


def _retire_old_literalbridge(path: str) -> str:
    """升级前把旧 jar 改名成 `.jar.bak-<时间>`（只改名、不删除），返回新路径；失败返回 ""。"""
    old = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = old.with_name(f"{old.name}.bak-{stamp}")
    index = 2
    while target.exists():
        target = old.with_name(f"{old.name}.bak-{stamp}-{index}")
        index += 1
    try:
        old.rename(target)
    except OSError:
        LOG.debug("Could not retire old LiteralBridge jar %s", old, exc_info=True)
        return ""
    return str(target)


def download_literalbridge(
    mods_dir: Path,
    *,
    loader: str = "",
    mc_version: str = "",
    timeout: float = 8.0,
    progress=None,
    upgrade: bool = False,
) -> dict[str, Any]:
    """尝试把 LiteralBridge 装进 mods_dir；upgrade=True 时顺带升到新版。

    返回 {'ok', 'reason', 'action', 'path', 'version', 'from_version', 'backup', 'page', 'tried'}：
      ok=True,  action=installed    → 之前没装，这次装上了
      ok=True,  action=upgraded     → 之前是旧版，已换新版（旧版改名备份，路径见 backup）
      ok=True,  action=up_to_date   → 已经是最新（或本地版本更高），什么都没动
      ok=False, reason=already_installed → 已装且未要求升级（upgrade=False）
      ok=False, reason=network      → 联网失败（列表拉不到 / 下载中断）
      ok=False, reason=no_release   → 拿到了，但一个带 jar 的 Release 都没有
      ok=False, reason=no_match     → 有 jar，但没有支持该「加载器 + MC 版本」的本体
      ok=False, reason=backup_failed→ 旧版改名失败；为免两份同 modId 崩游戏，放弃这次升级
    调用方据此决定是否回落到打开项目页。
    """
    page = LITERALBRIDGE_PAGE
    installed = installed_literalbridge(mods_dir)
    if installed and not upgrade:
        return {
            "ok": False, "reason": "already_installed", "page": page,
            "path": installed["path"], "version": installed["version"],
            "from_version": installed["version"],
        }
    installed_version = str((installed or {}).get("version") or "")
    try:
        releases = literalbridge_releases(timeout)
    except (OSError, ValueError) as exc:
        LOG.debug("LiteralBridge release query failed", exc_info=True)
        return {"ok": False, "reason": "network", "page": page, "detail": redact_secrets(str(exc))}
    if not releases:
        return {"ok": False, "reason": "no_release", "page": page}
    # GitHub 通常已按时间倒序，但别指望它 —— 显式按版本号从新到旧排，
    # 否则「取前 3 个 release 试」可能试到一个比本地还旧的版本。
    releases.sort(key=lambda item: version_key(item.get("version")), reverse=True)
    page = releases[0].get("page") or page
    # 已经装了的话，先用 Release 里的版本号做一次「要不要下」的预判：
    # 最新那个能选出资质的 Release 如果不比本地新，就根本不用下载。
    # （否则「本来已是最新」也得先下整份 jar —— 下载一失败还会被误报成网络错。）
    if installed:
        for release in releases[:3]:
            candidate = pick_literalbridge_asset(release, loader, mc_version)
            if not candidate:
                continue
            if version_key(release["version"]) <= version_key(installed_version):
                return {
                    "ok": True, "action": "up_to_date", "page": page,
                    "path": installed["path"], "version": installed_version,
                    "latest": release["version"], "from_version": installed_version,
                    "tried": [f"{release['version']} {candidate['name']}"],
                }
            break       # 有更新的候选 → 继续往下走真实下载 + 复核
    tried: list[str] = []
    for release in releases[:3]:
        asset = pick_literalbridge_asset(release, loader, mc_version)
        if not asset:
            continue
        tried.append(f"{release['version']} {asset['name']}")
        dest = Path(mods_dir) / asset["name"]
        backup = ""
        try:
            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / asset["name"]
                # 只轻量重试一次：再不行就交给调用方回落「打开项目页」，
                # 不为了成功率让用户干等（详见 download_release_asset 的说明）。
                download_release_asset(asset["url"], staged, timeout=60.0, progress=progress, attempts=2)
                if not _literalbridge_jar_matches(read_mod_jar_info(staged), loader, mc_version):
                    LOG.debug("LiteralBridge %s does not match %s/%s", asset["name"], loader, mc_version)
                    continue
                if installed and version_key(release["version"]) <= version_key(installed_version):
                    # 已装的就是最新的（或本地版本更高）—— 把刚下的那份丢掉，什么都不动。
                    return {
                        "ok": True, "action": "up_to_date", "page": page, "tried": tried,
                        "path": installed["path"], "version": installed_version,
                        "latest": release["version"], "from_version": installed_version,
                    }
                if installed:
                    backup = _retire_old_literalbridge(installed["path"])
                    if not backup:
                        return {
                            "ok": False, "reason": "backup_failed", "page": page,
                            "tried": tried, "version": installed_version,
                        }
                # 只有确认匹配才建 mods 目录 / 落盘，避免失败时留下空目录。
                Path(mods_dir).mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(dest))
        except (OSError, ValueError) as exc:
            LOG.debug("LiteralBridge download failed", exc_info=True)
            return {
                "ok": False, "reason": "network", "page": page,
                "tried": tried, "detail": redact_secrets(str(exc)),
            }
        return {
            "ok": True, "action": "upgraded" if installed else "installed",
            "path": str(dest), "version": release["version"], "name": asset["name"],
            "from_version": installed_version, "backup": backup,
            "page": page, "tried": tried,
        }
    return {"ok": False, "reason": "no_match", "page": page, "tried": tried}
