# 渡口通信

本目录是摆渡计划 GitHub Pages 发布页的可编辑源文件。网站只有一个自包含的 HTML，无需框架、字体服务或运行时密钥。

- `index.html` 是页面模板，页脚作者署名独立于发行目录。
- `src/depot.css`、`src/dispatch.css` 控制版式；`src/depot.js` 负责光标、版本筛选和 Issues 揭示板。
- `src/catalog.cjs` 处理公开发行和 README 的更新日志。桌面工作台与桥接模组按实际发行时间一起排序、分别筛选，测试版不会被遗漏。
- 在此目录依次运行 `node snapshot.mjs`、`node pack-y2k.mjs`；`FERRY_SITE_OUT` 指定输出目录。`public/` 只存随网站发布的图像。不应将接口密钥、原始截屏或个人信息放入此目录。
- 更新主分支的 README、网站、公开发行或成功完成发版构建时，`pages-sync.yml` 生成静态目录并请求发布到原有 `gh-pages`。浏览器也会核对公开发行信息；没有脚本或网络遇限时仍可使用静态目录。发帖和回复使用原仓库的 GitHub Issues。
