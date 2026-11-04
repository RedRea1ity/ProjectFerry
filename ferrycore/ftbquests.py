"""已废弃：摆渡不解析、不写回任何 SNBT。

FTB 任务书汉化改走 FTB Quest Localizer 的 `/ftblang export` 导出 JSON
（`kubejs/assets/ftbquestlocalizer/lang/en_us.json`），随后由
`ferrycore.json_components` + `ferrycore.scan` 处理富文本组件。
1.20.1 的 `config/ftbquests/quests/chapters/*.snbt` 内联文本一律不碰。

保留这个空模块只是为了给旧的 import 留个说明：它不再提供任何解析或写回功能。
确认仓库里没有旧引用后，可以整份删掉。
"""
