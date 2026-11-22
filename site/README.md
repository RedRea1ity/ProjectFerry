# 渡口通信

本目录是摆渡计划 GitHub Pages 发布页的可编辑源文件（Windows 9x 桌面窗口风格）。网站输出自包含的 `index.html` 与 `media/` 音轨目录，无需框架、字体服务或运行时密钥。

- `index.html` 是页面模板，页脚作者署名独立于发行目录。
- `src/depot.css`、`src/dispatch.css`、`src/gallery.css` 控制 Windows 9x 窗口外观、封面光盘与随身听、版本更新滚动广播条及渡口揭示板版式。
- `src/depot.js` 负责光盘隐藏音轨、特效开关、版本筛选、真实版本滚动广播和带 GitHub 头像/用户名的 Issues 论坛揭示板；`src/player.js` 负责渡口随身听与彩蛋音轨控制。
- `src/catalog.cjs` 处理公开发行、README 的更新日志与版本稳定性标注（如早期实验测试版提示），生成首页主版本卡片、站务滚动广播与版本更新链。桌面工作台与桥接模组按实际发行时间一起排序、分别筛选，测试版不会被遗漏。
- 在此目录依次运行 `node snapshot.mjs`、`node pack-y2k.mjs`；`FERRY_SITE_OUT` 指定输出目录。`public/` 只存随网站发布的图像和音频。不应将接口密钥、原始截屏或个人信息放入此目录。
- 更新主分支的 README、网站、公开发行或成功完成发版构建时，`pages-sync.yml` 生成静态目录并请求发布到 `gh-pages`。浏览器也会核对公开发行信息；没有脚本或网络遇限时仍可使用静态目录。揭示板支持绑定 GitHub 用户名或本地 Token 鉴权，在讨论串与回帖栏显示真实 GitHub 头像与用户名，所有主题与回复均同步至原仓库的 GitHub Issues。
