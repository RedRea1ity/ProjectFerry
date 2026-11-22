// Shared by the standalone HTML builder and its live GitHub reader. Only repository-owned URLs become links.
const FerryCatalog = (() => {
  const repo = 'https://github.com/RedRea1ity/ProjectFerry';
  const api = 'https://api.github.com/repos/RedRea1ity/ProjectFerry';

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));

  const date = t => new Date(Date.parse(t) + 8 * 3600000).toISOString().slice(0, 16).replace('T', ' ').replaceAll('-', '.');
  const size = n => `${(n / 1048576).toFixed(2)} MiB`;
  const tagLink = t => `${repo}/releases/tag/${encodeURIComponent(t)}`;
  const text = s => String(s || '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*|`/g, '')
    .trim();

  function normalize(rows) {
    if (!Array.isArray(rows)) throw Error('release list');
    return rows
      .filter(r => r && !r.draft && r.tag_name && Number.isFinite(Date.parse(r.published_at)))
      .map(r => {
        const tag = String(r.tag_name);
        const kind = /^bridge[-/]/i.test(tag) ? 'bridge' : /^v?\d+\./i.test(tag) ? 'app' : 'other';
        const assets = (r.assets || [])
          .filter(a => a.state === 'uploaded' && a.name && Number.isFinite(a.size))
          .map(a => ({
            name: String(a.name),
            size: a.size,
            sha: /^sha256:[0-9a-f]{64}$/i.test(a.digest || '') ? a.digest.slice(7) : ''
          }));
        const rawTitle = text(r.name || '');
        const title = rawTitle && rawTitle !== tag ? rawTitle.slice(0, 120) : '';
        const summary = text(
          (r.body || '')
            .split(/\r?\n/)
            .map(x => x.trim())
            .find(x => x && !/^#+ |full changelog|^https?:|^<!--|^\* \S+ by @/i.test(x)) || ''
        ).replace(/^[-*] /, '').slice(0, 280);
        return { tag, kind, at: r.published_at, pre: !!r.prerelease, title, assets, summary };
      })
      .sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.tag.localeCompare(b.tag));
  }

  function readNotes(md) {
    const out = Object.create(null);
    let tag = '', active = false;
    for (const ln of String(md || '').split(/\r?\n/)) {
      if (/^##\s+/.test(ln)) {
        active = /^##\s+(更新日志|版本记录|Changelog)/i.test(ln);
        tag = '';
        continue;
      }
      if (!active) continue;
      const m = ln.match(/^(?:\*\*|#{3,5}\s+)(v\d+\.\d+\.\d+(?:-[\w.]+)?)(?=[（(\s*：:]|$)/i);
      if (m) {
        tag = m[1];
        out[tag] ??= [];
        continue;
      }
      if (/^\*\*v|^###/.test(ln)) {
        tag = '';
        continue;
      }
      const single = ln.match(/^-\s*(v\d+\.\d+\.\d+)\s*[：:]\s*(.+)/);
      if (single) {
        out[single[1]] = [text(single[2])];
        continue;
      }
      if (tag && /^\s*-\s+/.test(ln) && out[tag].length < 3) {
        const s = text(ln.replace(/^\s*-\s+/, ''));
        if (s && !/[：:]$/.test(s)) out[tag].push(s);
      }
    }
    return out;
  }

  function readLabels(md) {
    const out = Object.create(null);
    let active = false;
    for (const ln of String(md || '').split(/\r?\n/)) {
      if (/^##\s+/.test(ln)) {
        active = /^##\s+(更新日志|版本记录|Changelog)/i.test(ln);
        continue;
      }
      if (!active) continue;
      const m = ln.match(/^(?:\*\*|#{3,5}\s+)(v\d+\.\d+\.\d+(?:-[\w.]+)?)\s*[（(]([^）)]+)[）)]/i);
      if (m) {
        const qual = text(m[2]);
        if (qual) out[m[1]] = qual.slice(0, 64);
      }
    }
    return out;
  }

  const annotation = {
    'v2.0.0-alpha.2': '通用游戏线扩展到吉里吉里、Godot 与掌机镜像。',
    'v2.0.0-alpha.1': '双轨工作台启航，加入 PC-98 镜像和双语译文表。',
    'v1.9.0-beta': '聊天栏桥接改用 Forge 事件；桌面端可直接导出映射。',
    'bridge-v1.0.0': '独立桥接模组，配合 Minecraft 1.20.1 Forge 使用。'
  };

  function versionBadge(r, labels = {}) {
    if (labels && labels[r.tag]) return labels[r.tag];
    if (r.title) {
      const m = r.title.match(/[（(]([^）)]+)[）)]/);
      if (m && m[1].trim()) return m[1].trim().slice(0, 64);
    }
    if (r.kind === 'bridge') return r.pre ? '桥接预发布' : '桥接正式版';
    if (r.pre) {
      if (/alpha/i.test(r.tag)) return '早期测试版';
      if (/beta/i.test(r.tag)) return '公开测试版';
      return '预发布测试版';
    }
    return '正式版';
  }

  const dl = (r, a) => `${repo}/releases/download/${encodeURIComponent(r.tag)}/${encodeURIComponent(a.name)}`;

  function clip(s, max = 44) {
    const clean = String(s || '').replace(/\s+/g, ' ').trim();
    return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
  }

  function broadcast(list, notes = {}, labels = {}) {
    const recent = (Array.isArray(list) ? list : []).slice(0, 6);
    if (!recent.length) {
      return '<div class="ticker-group"><span class="ticker-item">暂无公开发行记录，请前往 GitHub Releases 查看。</span></div>';
    }
    const items = recent.map(r => {
      const badge = versionBadge(r, labels);
      const warn = /不稳定|实验|alpha/i.test(badge) || /alpha/i.test(r.tag);
      const day = date(r.at).slice(0, 10);
      const rawDesc = annotation[r.tag] || notes[r.tag]?.[0] || r.summary || '';
      const shortDesc = clip(rawDesc.replace(/^⚠️?\s*(?:稳定性警示(?:[（(][^）)]*[）)])?[：:]\s*)?/, ''), 42);
      return `<a class="ticker-item${warn ? ' is-warn' : ''}" href="${tagLink(r.tag)}"><span class="ticker-date">[${day}]</span> <b>${esc(r.tag)}</b><span class="ticker-qual">（${esc(badge)}）</span>已更新${shortDesc ? ` — ${esc(shortDesc)}` : ''}</a>`;
    }).join('<span class="ticker-sep" aria-hidden="true">◆</span>');

    return `<div class="ticker-group">${items}<span class="ticker-sep" aria-hidden="true">◆</span></div><div class="ticker-group" aria-hidden="true">${items}<span class="ticker-sep">◆</span></div>`;
  }

  function head(list, labels = {}) {
    const r = list.find(x => x.kind === 'app');
    if (!r) return `<p>暂未找到桌面版。<a href="${repo}/releases">查看所有发行</a></p>`;
    const a = r.assets.find(x => /^ProjectFerry\.exe$/i.test(x.name));
    const stable = list.find(x => x.kind === 'app' && !x.pre);
    const badge = versionBadge(r, labels);
    const warn = r.pre && (/不稳定|实验|alpha/i.test(badge) || /alpha/i.test(r.tag));
    return `<div class="release"><div class="rel-meta"><b class="version">${esc(r.tag)}</b> <span class="stamp${warn ? ' stamp-warn' : ''}">${r.pre ? '预发布 · ' : ''}${esc(badge)}</span></div><h3>摆渡计划 · Windows 工作台</h3><p>通用游戏、Minecraft 模组，两条汉化路线。先体检，再校对；有人工译文的地方，让人工译文先过河。</p><div class="actions"><a class="mainbtn" id="current-download" href="${a ? esc(dl(r, a)) : tagLink(r.tag)}">${a ? '↓ 下载 Windows 免安装版' : '查看本版可用附件 ↗'}</a><a class="win-btn" href="${tagLink(r.tag)}">本版发行记录 ↗</a></div>${stable && stable.tag !== r.tag ? `<p class="stable">想用正式版？ <a href="${tagLink(stable.tag)}">最近的正式发行 ${esc(stable.tag)}</a></p>` : ''}</div><aside class="slip"><div class="sliptop"><span>随盘标签</span> <small>FILE LABEL</small></div><dl><div><dt>文件</dt><dd>${a ? esc(a.name) : '未附 Windows EXE'}</dd></div><div><dt>容量</dt><dd>${a ? size(a.size) : '—'}</dd></div><div><dt>入库</dt><dd>${date(r.at)} <small>UTC+8</small></dd></div></dl>${a?.sha ? `<details><summary>核对 SHA-256</summary><code>${esc(a.sha)}</code></details>` : '<small>本版未提供 SHA-256。</small>'}</aside>`;
  }

  function chain(list, notes = {}, labels = {}) {
    return list.map((r, i) => {
      const kinds = { app: '工作台', bridge: '桥接', other: '其他' };
      const desc = annotation[r.tag] || notes[r.tag]?.[0] || r.summary || '查看发行页中的文件与记录。';
      const qual = labels && labels[r.tag] ? ` · ${labels[r.tag]}` : '';
      return `<li data-kind="${r.kind}"><span class="disc-no" aria-hidden="true">${String(list.length - i).padStart(2, '0')}</span><div class="record"><div class="record-title"><a href="${tagLink(r.tag)}">${esc(r.tag)}</a><span class="label ${r.kind}${r.pre ? ' pre' : ''}">${kinds[r.kind]} · ${r.pre ? '预发布' : '正式'}${esc(qual)}</span></div><p>${esc(desc)}</p></div><time datetime="${esc(r.at)}">${date(r.at)}</time></li>`;
    }).join('');
  }

  function noteHTML(notes, tag) {
    const items = notes[tag] || [];
    return items.length
      ? `<ul>${items.map(s => `<li>${esc(s)}</li>`).join('')}</ul>`
      : '<p>README 暂无对应条目，请查阅本版发行记录。</p>';
  }

  return { repo, api, esc, date, normalize, readNotes, readLabels, versionBadge, broadcast, head, chain, noteHTML };
})();
if (typeof module !== 'undefined' && module.exports) module.exports = FerryCatalog;
