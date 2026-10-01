"""打包产物完整性：`engine/maskit-engine.spec` 必须覆盖全部引擎模块。

为什么单独一个文件：这类缺陷**只在打包态出现**，本地门禁与源码态永远绿。
`hiddenimports` 是逐个枚举引擎模块的（见 spec 里的既有条目与注释），而
`engine/selfcheck.py` 是 0.6.0 新增的 —— 加它的人（我）忘了同步 spec，
**源码态一切正常、打包态才会 ImportError**。

具体后果别夸大也别缩小：`panel.py` 里 `import selfcheck` 写在函数内（懒加载），
PyInstaller 的静态分析通常能捞到函数级 import，所以大概率仍会被打进包里；
但它**依赖分析器的运气**，而本项目其他 8 个引擎模块都显式枚举了 —— 漏一个就是
"打包机正常、用户机少功能"这一类最难排查的漂移（自检点了没反应，日志里只有
被 except 吞掉的导入错误）。所以：显式补上，并加这条守卫防止下一个新模块再漏。
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "engine" / "maskit-engine.spec"


class EngineSpecCoverageTests(unittest.TestCase):
    def _engine_modules(self):
        """engine/ 下的一级模块名（排除 spec 自身与包）。"""
        return {p.stem for p in (ROOT / "engine").glob("*.py")}

    def _spec_source(self):
        return SPEC.read_text(encoding="utf-8")

    def test_spec_exists_and_lists_hiddenimports(self):
        self.assertTrue(SPEC.is_file(), "找不到 %s" % SPEC)
        src = self._spec_source()
        self.assertIn("hiddenimports", src, "spec 里没有 hiddenimports？")
        self.assertIn("pathex=[str(ENGINE_DIR)", src.replace(" ", ""),
                      "pathex 必须含 ENGINE_DIR，否则引擎模块解析不到")

    def test_every_engine_module_is_reachable_from_the_spec(self):
        """每个引擎模块都要能被 spec 找到（在 hiddenimports 里，或被 import 链带到）。

        判据不是"字面出现在 hiddenimports 里"这么窄：被 spec 里已列出的模块
        **导入**到的模块本来就会被 PyInstaller 带上。所以这里两路合并：
          ① hiddenimports 字面列出的；
          ② 从那些模块出发做一次导入闭包（含 `from x import y` 与 `import x`）。
        两条都不覆盖的引擎模块 = 打包态可能缺失，必须报出来。
        """
        modules = self._engine_modules()
        src = self._spec_source()
        listed = set(re.findall(r"'([a-z_]+)'", src)) & modules
        self.assertIn("panel", listed, "解析不到 hiddenimports 列表（spec 格式变了？）")

        # 导入闭包：把 engine/ 下的模块 import 关系走一遍
        declared = set(listed) | {"engine_entry"}
        seen = set()
        frontier = list(declared)
        while frontier:
            name = frontier.pop()
            if name in seen:
                continue
            seen.add(name)
            path = ROOT / "engine" / (name + ".py")
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        frontier.append(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    frontier.append(node.module.split(".")[0])

        missing = sorted(m for m in modules if m not in seen and m not in listed)
        self.assertEqual(missing, [],
                         "这些引擎模块既没在 hiddenimports 里、也不在任何已列模块的导入链上："
                         "%s（打包态可能缺模块，源码态完全看不出来）" % missing)

    def test_transport_modules_are_shipped_as_source(self):
        src = self._spec_source()
        for name in ("connection_policy", "mitm_transport_adapter"):
            self.assertIn("'" + name + "'", src)
            self.assertIn("ENGINE_DIR / '" + name + ".py'", src)

    def test_selfcheck_is_explicitly_listed(self):
        """`selfcheck` 必须**显式**在 hiddenimports 里（0.6.0 漏过一次）。

        它由 panel 在函数内懒加载，且被三处调用点包在 try 里 —— 万一打包态缺它，
        表现是"自检点了没反应"而不是报错。所以即使导入闭包能覆盖它，也要求显式列出。
        """
        src = self._spec_source()
        self.assertRegex(src, r"hiddenimports[\s\S]{0,800}?'selfcheck'",
                         "hiddenimports 里没有 'selfcheck'（0.6.0 新增模块漏登记过）")


if __name__ == "__main__":
    unittest.main()
