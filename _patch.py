# 临时补丁：硬编码文本接入翻译流水线（桥接用）（跑完即删）
import json
import pathlib

print('core 段已应用，跳过')


# ---------- GUI ----------
gui = pathlib.Path("ProjectFerry.pyw")
src = gui.read_text(encoding="utf-8")

# 5) 设置勾选框
old = '        ttk.Checkbutton(actions, text="填补人工汉化缺失", variable=self.fill_var, command=self._refine_changed).pack(side="left", padx=(12, 0))'
new = old + '''
        self.hardcoded_var = tk.BooleanVar(value=bool(self.config.get("translate_hardcoded", True)))
        ttk.Checkbutton(actions, text="翻译硬编码文本（桥接用）", variable=self.hardcoded_var, command=self._refine_changed).pack(side="left", padx=(12, 0))'''
assert src.count(old) == 1, "checkbox"
src = src.replace(old, new)

# 6) 快照带上开关
old = '        config["community_enabled"] = bool(self.community_var.get())'
assert src.count(old) == 1
src = src.replace(old, old + '\n        config["translate_hardcoded"] = bool(self.hardcoded_var.get())')

# 7) 扫描线程：接收开关，硬编码专属模组也进翻译目标
old = "        threading.Thread(target=self._scan_worker, args=(self.fill_var.get(), self.refine_var.get(), self.path_var.get().strip()), daemon=True).start()"
assert src.count(old) == 1
src = src.replace(old, "        threading.Thread(target=self._scan_worker, args=(self.fill_var.get(), self.refine_var.get(), self.path_var.get().strip(), self.hardcoded_var.get()), daemon=True).start()")
old = "    def _scan_worker(self, fill_community: bool, refine_community: bool, manual: str) -> None:"
assert src.count(old) == 1
src = src.replace(old, "    def _scan_worker(self, fill_community: bool, refine_community: bool, manual: str, translate_hardcoded: bool = True) -> None:")
old = '''                        if not is_excluded and st.modid not in uninstalled[str(instance)] and ((st.missing > 0 and (not st.has_community or fill_community or refine_community)) or st.modid in patch_pending):
                            translatable_targets.setdefault(str(instance), set()).add(st.modid)'''
new = '''                        if not is_excluded and st.modid not in uninstalled[str(instance)] and ((st.missing > 0 and (not st.has_community or fill_community or refine_community)) or st.modid in patch_pending or (translate_hardcoded and st.modid in hardcoded_namespaces)):
                            translatable_targets.setdefault(str(instance), set()).add(st.modid)'''
assert src.count(old) == 1, "scan targets"
src = src.replace(old, new)

# 8) 翻译线程：收集硬编码候选，进翻译批
old = '''                    baseline = core.fetch_community_baseline(candidate_scan, config, Path(settings["cache_dir"]), modids)'''
assert src.count(old) == 1
old2 = '''                entries.extend(patch_entries)
                entries = core.skip_ignored_entries(entries, config)
                if not entries and not baseline:
                    continue'''
new = '''                entries.extend(patch_entries)
                # 硬编码候选（桥接用）：key 是 nbt:<hash>，译文存 sidecar，不写进资源包。
                hardcoded_entries: list[core.Entry] = []
                if config.get("translate_hardcoded", True):
                    existing_hardcoded = core.load_hardcoded_translations(pack)
                    hardcoded_entries = core.missing_hardcoded_entries(
                        core.hardcoded_strings_by_modid(scan, self._hardcoded_cache.get(instance_path) or [], modids),
                        existing_hardcoded, set(modids))
                entries.extend(hardcoded_entries)
                entries = core.skip_ignored_entries(entries, config)
                if not entries and not baseline:
                    continue'''
assert src.count(old2) == 1, "worker extend"
src = src.replace(old2, new)

# pack 提前定义（原在写入块里才定义）
old = '''                core.yield_to_community(scan, Path(settings["output_dir"]) / settings["pack_name"])'''
assert src.count(old) == 1
src = src.replace(old, '''                pack = Path(settings["output_dir"]) / settings["pack_name"]
                ''' + old)

# 翻译完成后：收拢 nbt 译文进 sidecar；lang 包过滤掉 nbt 键
old = '''                all_errors.extend(errors)
                core.update_progress(translations, failed)
                if translations or baseline:
                    pack = Path(settings["output_dir"]) / settings["pack_name"]
                    lang_translations = {modid: dict(data) for modid, data in baseline.items()}
                    for modid, data in translations.items():
                        lang_translations.setdefault(modid, {}).update({key: value for key, value in data.items() if not key.startswith("patchouli:")})'''
new = '''                all_errors.extend(errors)
                core.update_progress(translations, failed)
                if hardcoded_entries:
                    nbt_pairs = core.hardcoded_pairs_from_entries(hardcoded_entries, translations)
                    if nbt_pairs:
                        merged_hardcoded = core.load_hardcoded_translations(pack)
                        for nbt_modid, pairs in nbt_pairs.items():
                            merged_hardcoded.setdefault(nbt_modid, {}).update(pairs)
                        core.save_hardcoded_translations(pack, merged_hardcoded)
                if translations or baseline:
                    lang_translations = {modid: dict(data) for modid, data in baseline.items()}
                    for modid, data in translations.items():
                        lang_translations.setdefault(modid, {}).update({key: value for key, value in data.items() if not key.startswith(("patchouli:", "nbt:"))})'''
assert src.count(old) == 1, "post translate"
src = src.replace(old, new)

# 9) 双击预检计数 + 翻译守卫：把硬编码候选也算进去
old = '''            entries = core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            )
            entries.extend(core.missing_patchouli_entries(scan, modids))
            entries = core.skip_ignored_entries(entries, config)
            total += len(entries)'''
new = '''            entries = core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            )
            entries.extend(core.missing_patchouli_entries(scan, modids))
            if config.get("translate_hardcoded", True):
                pack = Path(config.get("output_dir") or Path(path) / "resourcepacks") / (config.get("pack_name") or core.DEFAULT_CONFIG["pack_name"])
                entries.extend(core.missing_hardcoded_entries(
                    core.hardcoded_strings_by_modid(scan, self._hardcoded_cache.get(path) or [], modids),
                    core.load_hardcoded_translations(pack), modids))
            entries = core.skip_ignored_entries(entries, config)
            total += len(entries)'''
assert src.count(old) == 1, "pending count"
src = src.replace(old, new)

old = '''        cached = [(self.scan_cache.get(path), modids) for path, modids in targets.items()]
        if all(scan is not None for scan, _ in cached) and not any(
            core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            ) or core.missing_patchouli_entries(scan, modids)
            for scan, modids in cached
        ):'''
new = '''        cached = [(path, self.scan_cache.get(path), modids) for path, modids in targets.items()]
        if all(scan is not None for _, scan, _ in cached) and not any(
            core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            ) or core.missing_patchouli_entries(scan, modids)
            or (config.get("translate_hardcoded", True) and core.has_pending_hardcoded(self._hardcoded_cache.get(path) or [], scan, modids))
            for path, scan, modids in cached
        ):'''
assert src.count(old) == 1, "start translate guard"
src = src.replace(old, new)
old = '            message = "所选模组没有需要翻译的非空英文 key；已有汉化或 AI 已覆盖全部有效条目。"'
assert src.count(old) == 1
src = src.replace(old, '            message = "所选模组没有需要翻译的英文 key 或硬编码文本；已有汉化或 AI 已覆盖全部有效条目。"')

# 10) 导出桥接映射：合并 sidecar
old = '''        try:
            settings = core.resolve_translate_settings(self._snapshot_config(), Path(instance_value))
            pack = Path(settings["output_dir"]) / settings["pack_name"]
            ai_by_modid = core.load_pack_translations(pack)
        except (OSError, ValueError):
            ai_by_modid = {}
        mapping = core.build_bridge_mapping(scan.english, scan.community, ai_by_modid)'''
new = '''        try:
            settings = core.resolve_translate_settings(self._snapshot_config(), Path(instance_value))
            pack = Path(settings["output_dir"]) / settings["pack_name"]
            ai_by_modid = core.load_pack_translations(pack)
        except (OSError, ValueError):
            pack = None
            ai_by_modid = {}
        mapping = core.build_bridge_mapping(scan.english, scan.community, ai_by_modid,
                                            hardcoded=core.load_hardcoded_translations(pack) if pack else None)'''
assert src.count(old) == 1, "export merge"
src = src.replace(old, new)

# 11) 汉化限制弹窗：第三元素现在是全量候选，样例取前 3
old = '''                for sample in samples:'''
assert src.count(old) == 1
src = src.replace(old, '''                for sample in samples[:3]:''')

# 12) 使用说明
old = '''            "右键「加入不翻译名单」：此模组完全跳过 AI 翻译（状态列显示 ∅），右键可移出恢复。",'''
new = '''            "右键「加入不翻译名单」：此模组完全跳过 AI 翻译（状态列显示 ∅），右键可移出恢复。",
            "勾选「翻译硬编码文本」后，疑似硬编码的英文会随翻译批一起交给 AI，译文存在资源包旁的 .hardcoded.json，由摆渡桥在游戏里做显示替换。",'''
assert src.count(old) == 1, "help"
src = src.replace(old, new)

gui.write_text(src, encoding="utf-8", newline="")
print("gui ok")

# ---------- example 配置 ----------
example = pathlib.Path("ferry_config.example.json")
cfg = json.loads(example.read_text(encoding="utf-8"))
cfg["translate_hardcoded"] = True
example.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("example ok")
