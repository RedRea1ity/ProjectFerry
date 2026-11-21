// Shared by the standalone HTML builder and its live GitHub reader. Only repository-owned URLs become links.
const FerryCatalog = (() => {
  const repo='https://github.com/RedRea1ity/ProjectFerry', api='https://api.github.com/repos/RedRea1ity/ProjectFerry';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date=t=>new Date(Date.parse(t)+8*3600000).toISOString().slice(0,16).replace('T',' ').replaceAll('-','.');
  const size=n=>`${(n/1048576).toFixed(2)} MiB`;
  const tagLink=t=>`${repo}/releases/tag/${encodeURIComponent(t)}`;
  const text=s=>String(s||'').replace(/!\[[^\]]*\]\([^)]*\)/g,'').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/\*\*|`/g,'').trim();
  function normalize(rows){
    if(!Array.isArray(rows)) throw Error('release list');
    return rows.filter(r=>r&&!r.draft&&r.tag_name&&Number.isFinite(Date.parse(r.published_at))).map(r=>{
      const tag=String(r.tag_name), kind=/^bridge[-/]/i.test(tag)?'bridge':/^v?\d+\./i.test(tag)?'app':'other';
      const assets=(r.assets||[]).filter(a=>a.state==='uploaded'&&a.name&&Number.isFinite(a.size)).map(a=>({name:String(a.name),size:a.size,sha:/^sha256:[0-9a-f]{64}$/i.test(a.digest||'')?a.digest.slice(7):''}));
      return {tag,kind,at:r.published_at,pre:!!r.prerelease,assets,summary:text((r.body||'').split(/\r?\n/).map(x=>x.trim()).find(x=>x&&!/^#+ |full changelog|^https?:|^<!--|^\* \S+ by @/i.test(x))||'').replace(/^[-*] /,'').slice(0,280)};
    }).sort((a,b)=>Date.parse(b.at)-Date.parse(a.at)||a.tag.localeCompare(b.tag));
  }
  function readNotes(md){
    const out=Object.create(null); let tag='',active=false;
    for(const ln of md.split(/\r?\n/)){
      if(/^##\s+/.test(ln)){active=/^##\s+(更新日志|版本记录|Changelog)/i.test(ln);tag='';continue}
      if(!active)continue;
      const m=ln.match(/^(?:\*\*|#{3,5}\s+)(v\d+\.\d+\.\d+(?:-[\w.]+)?)(?=[（(\s*：:]|$)/i);
      if(m){tag=m[1];out[tag]??=[];continue}
      if(/^\*\*v|^###/.test(ln)){tag='';continue}
      const single=ln.match(/^-\s*(v\d+\.\d+\.\d+)\s*[：:]\s*(.+)/);
      if(single){out[single[1]]=[text(single[2])];continue}
      if(tag&&/^\s*-\s+/.test(ln)&&out[tag].length<3){const s=text(ln.replace(/^\s*-\s+/,''));if(s&&!/[：:]$/.test(s))out[tag].push(s)}
    }
    return out;
  }
  const annotation={ 'v2.0.0-alpha.2':'通用游戏线扩展到吉里吉里、Godot 与掌机镜像。', 'v2.0.0-alpha.1':'双轨工作台启航，加入 PC-98 镜像和双语译文表。', 'v1.9.0-beta':'聊天栏桥接改用 Forge 事件；桌面端可直接导出映射。', 'bridge-v1.0.0':'独立桥接模组，配合 Minecraft 1.20.1 Forge 使用。' };
  const dl=(r,a)=>`${repo}/releases/download/${encodeURIComponent(r.tag)}/${encodeURIComponent(a.name)}`;
  function head(list){
    const r=list.find(x=>x.kind==='app'); if(!r)return `<p>暂未找到桌面版。<a href="${repo}/releases">查看所有发行</a></p>`;
    const a=r.assets.find(x=>/^ProjectFerry\.exe$/i.test(x.name)), stable=list.find(x=>x.kind==='app'&&!x.pre);
    return `<div class="release"><div><b class="version">${esc(r.tag)}</b> <span class="stamp">${r.pre?'预发布 · 测试版':'正式版'}</span></div><h3>摆渡计划 · Windows 工作台</h3><p>通用游戏、Minecraft 模组，两条汉化路线。先体检，再校对；有人工译文的地方，让人工译文先过河。</p><div class="actions"><a class="mainbtn" id="current-download" href="${a?esc(dl(r,a)):tagLink(r.tag)}">${a?'↓ 下载 Windows 免安装版':'查看本版可用附件 ↗'}</a><a href="${tagLink(r.tag)}">本版发行记录 ↗</a></div>${stable&&stable.tag!==r.tag?`<p class="stable">想用正式版？ <a href="${tagLink(stable.tag)}">最近的正式发行 ${esc(stable.tag)}</a></p>`:''}</div><aside class="slip"><div class="sliptop">随盘标签 <span>FILE LABEL</span></div><dl><div><dt>文件</dt><dd>${a?esc(a.name):'未附 Windows EXE'}</dd></div><div><dt>容量</dt><dd>${a?size(a.size):'—'}</dd></div><div><dt>入库</dt><dd>${date(r.at)} <small>UTC+8</small></dd></div></dl>${a?.sha?`<details><summary>核对 SHA-256</summary><code>${esc(a.sha)}</code></details>`:'<small>本版未提供 SHA-256。</small>'}</aside>`;
  }
  function chain(list,notes={}){return list.map((r,i)=>{
    const kinds={app:'工作台',bridge:'桥接',other:'其他'}, desc=annotation[r.tag]||notes[r.tag]?.[0]||r.summary||'查看发行页中的文件与记录。';
    return `<li data-kind="${r.kind}"><span class="disc-no" aria-hidden="true">${String(list.length-i).padStart(2,'0')}</span><div class="record"><div class="record-title"><a href="${tagLink(r.tag)}">${esc(r.tag)}</a><span class="label ${r.kind}">${kinds[r.kind]} · ${r.pre?'预发布':'正式'}</span></div><p>${esc(desc)}</p></div><time datetime="${esc(r.at)}">${date(r.at)}</time></li>`;
  }).join('')}
  function noteHTML(notes,tag){const items=notes[tag]||[];return items.length?`<ul>${items.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`:'<p>README 暂无对应条目，请查阅本版发行记录。</p>'}
  return {repo,api,esc,date,normalize,readNotes,head,chain,noteHTML};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=FerryCatalog;
