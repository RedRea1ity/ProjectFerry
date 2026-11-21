# 渡口通信

本目录是摆渡计划 GitHub Pages 发布页的可编辑源文件。页面和两首按需播放的 MP3 均为静态文件，无需框架、字体服务或运行时密钥。

- `index.html` 是页面模板，页脚作者署名独立于发行目录。
- `src/depot.css`、`src/dispatch.css` 控制版式；`src/depot.js` 负责光盘彩蛋、相册、版本筛选和 Issues 揭示板。
- `src/gallery.css` 的静态扫描线只覆盖封面 CG；`src/player.js` 为随身听和彩蛋音乐接上同款播放面板，关闭 JavaScript 时保留原生音频控件。
- `src/catalog.cjs` 处理公开发行和 README 的更新日志。桌面工作台与桥接模组按实际发行时间一起排序、分别筛选，测试版不会被遗漏。
- 在此目录依次运行 `node snapshot.mjs`、`node pack-y2k.mjs`；`FERRY_SITE_OUT` 指定输出目录。`public/` 存随网站发布的图像，`public/media/` 存两首内置音频。访客添加的音乐仅由本地浏览器播放。不应将接口密钥、原始截屏或个人信息放入此目录。
- 更新主分支的 README、网站、公开发行或成功完成发版构建时，`pages-sync.yml` 生成静态目录并请求发布到原有 `gh-pages`。浏览器也会核对公开发行信息；没有脚本或网络遇限时仍可使用静态目录。发帖和回复使用原仓库的 GitHub Issues。
