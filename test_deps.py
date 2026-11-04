"""依赖体检与安装的测试。

不能依赖本机真装了 MinGW（CI 上肯定没有），所以全部用临时目录造出
"看起来像装好了"的目录结构，再用 `file://` 走真实的下载解压链路。
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ferrycore import deps
from ferrycore.deps import Dependency


def make_fake_dep(root: Path, dep_id: str = "fakedep", bin_name: str = "fake-gcc.exe") -> Dependency:
    return Dependency(
        id=dep_id,
        name=f"假依赖 {dep_id}",
        why="测试用",
        size_mb=1.0,
        binaries=(bin_name,),
        target=root / dep_id,
    )


class LocateTests(unittest.TestCase):
    def test_missing_when_nothing_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = make_fake_dep(Path(tmp))
            self.assertIsNone(dep.locate())
            self.assertFalse(dep.installed)

    def test_found_in_target_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = make_fake_dep(Path(tmp))
            (dep.target / "bin").mkdir(parents=True)
            exe = dep.target / "bin" / "fake-gcc.exe"
            exe.write_bytes(b"MZ")
            self.assertEqual(dep.locate(), exe)
            self.assertTrue(dep.installed)

    def test_found_in_target_root(self):
        """有些工具链 exe 直接摆在根目录，不该因为多一层 bin 就认不出。"""
        with tempfile.TemporaryDirectory() as tmp:
            dep = make_fake_dep(Path(tmp))
            dep.target.mkdir(parents=True)
            exe = dep.target / "fake-gcc.exe"
            exe.write_bytes(b"MZ")
            self.assertEqual(dep.locate(), exe)

    def test_found_via_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = make_fake_dep(Path(tmp), bin_name="definitely-not-a-real-binary.exe")
            fake = Path(tmp) / "elsewhere.exe"
            fake.write_bytes(b"MZ")
            with patch.object(deps.shutil, "which", return_value=str(fake)):
                self.assertEqual(dep.locate(), fake)


class ArchitectureTests(unittest.TestCase):
    """32 位和 64 位必须是两个依赖 —— 混用会让游戏连启动都不启动，且不报错。"""

    def test_mingw32_and_64_are_distinct(self):
        d32 = deps.REGISTRY["mingw32"]
        d64 = deps.REGISTRY["mingw64"]
        self.assertNotEqual(d32.binaries, d64.binaries)
        self.assertNotEqual(d32.target, d64.target)
        self.assertIn("i686-w64-mingw32-gcc.exe", d32.binaries)
        self.assertIn("x86_64-w64-mingw32-gcc.exe", d64.binaries)

    def test_installing_one_does_not_satisfy_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d32 = deps._mingw("32")
            d64 = deps._mingw("64")
            # 只造出 32 位那一份
            (root / "mingw32" / "bin").mkdir(parents=True)
            (root / "mingw32" / "bin" / "i686-w64-mingw32-gcc.exe").write_bytes(b"MZ")
            with patch.multiple(
                deps,
                REGISTRY={
                    "mingw32": _with_target(d32, root / "mingw32"),
                    "mingw64": _with_target(d64, root / "mingw64"),
                },
            ), patch.object(deps.shutil, "which", return_value=None):
                status = deps.resolve(["mingw32", "mingw64"])
                self.assertEqual([d.id for d in status.missing], ["mingw64"])


def _with_target(dep: Dependency, target: Path) -> Dependency:
    """复制一份只改 target 的依赖（frozen dataclass，不能原地改）。"""
    import dataclasses

    return dataclasses.replace(dep, target=target)


class ResolveTests(unittest.TestCase):
    def test_empty_needs_nothing(self):
        status = deps.resolve([])
        self.assertTrue(status.ok)
        self.assertEqual(status.summary(), "")

    def test_unknown_id_is_reported_not_ignored(self):
        """适配器要了一个摆渡不认识的依赖，说明版本对不上，不能当没事。"""
        status = deps.resolve(["no-such-dependency"])
        self.assertFalse(status.ok)
        self.assertEqual(status.unknown, ["no-such-dependency"])
        self.assertIn("未知依赖", status.summary())

    def test_installed_dependency_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = make_fake_dep(Path(tmp))
            (dep.target / "bin").mkdir(parents=True)
            (dep.target / "bin" / "fake-gcc.exe").write_bytes(b"MZ")
            with patch.object(deps, "REGISTRY", {"fakedep": dep}):
                self.assertTrue(deps.resolve(["fakedep"]).ok)


class PromptTests(unittest.TestCase):
    def test_prompt_states_reason_and_size(self):
        """弹窗必须能回答"为什么"，不能只说缺什么。"""
        dep = deps.REGISTRY["mingw32"]
        text = dep.prompt()
        self.assertIn("MinGW-w64 32 位编译器", text)
        self.assertIn("Shift-JIS", text)
        self.assertIn("MB", text)
        self.assertIn(str(dep.target), text)


class InstallTests(unittest.TestCase):
    def test_install_from_zip_with_wrapper_dir(self):
        """压缩包带一层同名目录时，要剥掉，否则 exe 会埋在两层下面。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "pkg.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("pkg/bin/fake-gcc.exe", b"MZ")
                zf.writestr("pkg/README.txt", b"hi")

            dep = Dependency(
                id="zipped",
                name="zip 装的依赖",
                why="测试",
                size_mb=0.1,
                binaries=("fake-gcc.exe",),
                target=root / "installed",
                zip_url=archive.as_uri(),
            )
            ok, message = deps.install(dep)
            self.assertTrue(ok, message)
            self.assertTrue((dep.target / "bin" / "fake-gcc.exe").is_file())
            self.assertFalse((dep.target / "pkg").exists(), "外层目录没剥掉")
            self.assertTrue(dep.installed)

    def test_install_reports_failure_instead_of_raising(self):
        """下载挂了要返回失败信息，不能把异常扔到 GUI 里。"""
        with tempfile.TemporaryDirectory() as tmp:
            dep = Dependency(
                id="broken",
                name="坏依赖",
                why="测试",
                size_mb=0.1,
                binaries=("nope.exe",),
                target=Path(tmp) / "x",
                zip_url=(Path(tmp) / "does-not-exist.zip").as_uri(),
            )
            ok, message = deps.install(dep)
            self.assertFalse(ok)
            self.assertIn("下载失败", message)

    def test_install_reports_when_binary_still_missing(self):
        """解压成功但结构不对时，要说清楚，而不是报成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "odd.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("something/other.exe", b"MZ")
            dep = Dependency(
                id="odd",
                name="结构不对的依赖",
                why="测试",
                size_mb=0.1,
                binaries=("fake-gcc.exe",),
                target=root / "inst",
                zip_url=archive.as_uri(),
            )
            ok, message = deps.install(dep)
            self.assertFalse(ok)
            self.assertIn("找不到", message)


class CommonPrefixTests(unittest.TestCase):
    def test_single_wrapper_dir_stripped(self):
        self.assertEqual(deps._common_prefix(["pkg/a", "pkg/b"]), "pkg/")

    def test_no_wrapper_when_files_at_root(self):
        self.assertEqual(deps._common_prefix(["a/b", "c/d"]), "")

    def test_partial_prefix_is_not_stripped(self):
        """只有一个条目碰巧在子目录里时不能剥，否则会把文件搬错地方。"""
        self.assertEqual(deps._common_prefix(["pkg/a", "top.txt"]), "")


class LocalizeGateTests(unittest.TestCase):
    """缺依赖时必须在 collect 之前停手，而不是翻完三千行再说装不上。"""

    def _adapter(self, needs, collected: list):
        from ferrycore.adapters.base import Adapter, ApplyReport, ProbeReport

        class NeedsAdapter(Adapter):
            name = "test-needs"

            def probe(self, root):
                # writable=False 是故意的：真实场景里 HSP 在没编译器时
                # 报的就是这一项，用户会误以为这本游戏根本翻不了
                return ProbeReport(
                    engine="test",
                    root=root,
                    readable=True,
                    writable=False,
                    unit_count=5,
                    needs=needs,
                )

            def collect(self, root):
                collected.append(root)
                return []

            def apply(self, root, translations, out=None):
                return ApplyReport()

        return NeedsAdapter()

    def _run(self, needs, collected, status_registry):
        from ferrycore.localize import localize_game

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(deps, "REGISTRY", status_registry):
                return localize_game(Path(tmp), adapter=self._adapter(needs, collected))

    def test_missing_dependency_blocks_before_collect(self):
        # 用系统临时目录造一个保证不存在的路径，不写死 D 盘（CI 跑在 Linux 上）
        dep = make_fake_dep(Path(tempfile.gettempdir()) / "ferry-no-such-dep-root")
        collected: list = []
        result = self._run(["fakedep"], collected, {"fakedep": dep})
        self.assertTrue(result.blocked_by_deps)
        self.assertEqual([d.id for d in result.missing_deps], ["fakedep"])
        self.assertEqual(collected, [], "缺依赖却已经去抽文本了")
        self.assertFalse(result.ok)

    def test_summary_puts_dependency_first(self):
        """缺依赖时第一行必须是它，不能埋在"写不回去"后面。"""
        # 用系统临时目录造一个保证不存在的路径，不写死 D 盘（CI 跑在 Linux 上）
        dep = make_fake_dep(Path(tempfile.gettempdir()) / "ferry-no-such-dep-root")
        result = self._run(["fakedep"], [], {"fakedep": dep})
        first = result.summary().splitlines()[0]
        self.assertIn("缺依赖", first)
        self.assertIn("假依赖", first)

    def test_unknown_dependency_also_blocks(self):
        """适配器要了不认识的依赖，不能当没这回事继续跑。"""
        collected: list = []
        result = self._run(["mystery-tool"], collected, {})
        self.assertTrue(result.blocked_by_deps)
        self.assertEqual(collected, [])
        self.assertIn("未知依赖", result.summary())

    def test_satisfied_dependency_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = make_fake_dep(Path(tmp))
            (dep.target / "bin").mkdir(parents=True)
            (dep.target / "bin" / "fake-gcc.exe").write_bytes(b"MZ")
            collected: list = []
            result = self._run(["fakedep"], collected, {"fakedep": dep})
            self.assertFalse(result.blocked_by_deps)
            # writable=False 仍然会被 ready 拦住，但那是另一回事了
            self.assertEqual(result.entries, 0)
            self.assertFalse(any("缺依赖" in line for line in result.summary().splitlines()))


if __name__ == "__main__":
    unittest.main()
