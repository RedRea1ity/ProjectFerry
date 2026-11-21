// The cover is an enhancement: reading and downloading never wait for motion.
// CD bonus track. Keep normal clicks inert; assistive activation has no click count.
(() => {
  const trigger = document.querySelector('.disc-trigger');
  const dialog = document.querySelector('#bonus-track');
  if (!trigger || !dialog?.showModal) return;
  const pictures = dialog.querySelectorAll('.bonus-screen img');
  const letter = dialog.querySelector('.bonus-letter');
  const count = dialog.querySelector('.bonus-count');
  const next = dialog.querySelector('.bonus-next');
  let timer, track = 0, lastTap = null;

  function show(index) {
    clearTimeout(timer);
    track = index;
    pictures[0].hidden = index !== 0;
    pictures[1].hidden = index === 0;
    letter.hidden = index !== 2;
    count.textContent = ['01 / 03', '02 / 03', '03 / 03'][index];
    next.textContent = index === 2 ? '重播 ↺' : '下一轨 ▸';
    if (index < 2) timer = setTimeout(() => { if (dialog.open) show(index + 1); }, index === 0 ? 1800 : 2600);
  }
  function playBonus() {
    if (dialog.open) return;
    lastTap = null;
    dialog.showModal();
    show(0);
  }
  trigger.hidden = false;
  trigger.addEventListener('dblclick', playBonus);
  trigger.addEventListener('click', event => { if (event.detail === 0) playBonus(); });
  trigger.addEventListener('pointerup', event => {
    if (event.pointerType !== 'touch' || !event.isPrimary) return;
    const {timeStamp: t, clientX: x, clientY: y} = event;
    if (lastTap && t - lastTap.t < 450 && Math.hypot(x - lastTap.x, y - lastTap.y) < 28) playBonus();
    else lastTap = {t, x, y};
  });
  trigger.addEventListener('pointercancel', () => { lastTap = null; });
  next.addEventListener('click', () => show((track + 1) % 3));
  dialog.addEventListener('close', () => { clearTimeout(timer); if (!dialog.open) trigger.focus({preventScroll:true}); });
})();
(() => {
  const dialog=document.querySelector('#art-view');if(!dialog||!dialog.showModal)return;
  const enlarged=dialog.querySelector('img');let opener;
  for(const button of document.querySelectorAll('[data-zoom]')){button.hidden=false;button.addEventListener('click',()=>{
    const figure=button.closest('figure'),img=figure.querySelector('img');opener=button;
    document.querySelector('#art-caption').textContent=figure.querySelector('figcaption span').textContent;
    enlarged.src=img.currentSrc||img.src;enlarged.alt=img.alt;dialog.showModal();
  })}dialog.addEventListener('close',()=>{enlarged.removeAttribute('src');opener?.focus()});
})();
const cover=document.querySelector('.cover'), replay=document.querySelector('#replay');
const motion=matchMedia('(prefers-reduced-motion: reduce)');
const trades=[['.issue','校对','#8f3760'],['.latin','排字','#126475'],['.motto','入库','#655183']];
let workers=[],animations=[],manualOff=false;
try{manualOff=localStorage.getItem('ferry-fx')==='off'}catch{}
const fxSwitch=document.querySelector('#fx-switch');
function clear(){for(const a of animations)a.cancel();animations=[];for(const w of workers)w.remove();workers=[]}
function play(){
  clear();if(manualOff||motion.matches||!Element.prototype.animate)return;
  const bounds=cover.getBoundingClientRect();
  trades.forEach(([selector,label,color],i)=>{
    const target=cover.querySelector(selector), r=target.getBoundingClientRect();
    const x=r.left-bounds.left+Math.min(r.width-12,90+15*i), y=r.top-bounds.top+r.height-2;
    const w=document.createElement('span');w.className='worker';w.setAttribute('aria-hidden','true');w.style.setProperty('--cursor',color);w.style.color=color;
    const tag=document.createElement('b');tag.textContent=label;w.append(tag);cover.append(w);workers.push(w);
    const pos=(a,b)=>`translate(${a}px,${b}px)`;
    const a=w.animate([{transform:pos(bounds.width*.76,16+32*i),opacity:0},{offset:.12,opacity:1},{offset:.52,transform:pos(x-22,y),opacity:1},{offset:.78,transform:pos(x,y),opacity:1},{transform:pos(x+28,y-9),opacity:0}],{duration:1820,delay:i*155,fill:'both',easing:'ease-in-out'});
    animations.push(a);a.onfinish=()=>w.remove();
    animations.push(target.animate([{outline:'1px solid transparent'},{outline:`1px solid ${color}`},{outline:'1px solid transparent'}],{duration:720,delay:820+i*155}));
  });
}
function sync(){clear();const on=!manualOff&&!motion.matches;document.documentElement.classList.toggle('fx-on',on);replay.hidden=!on||!Element.prototype.animate;fxSwitch.hidden=false;fxSwitch.disabled=motion.matches;fxSwitch.setAttribute('aria-pressed',String(on));fxSwitch.textContent=motion.matches?'系统已减弱动态':`特效：${on?'开':'关'}`}
fxSwitch.addEventListener('click',()=>{manualOff=!manualOff;try{localStorage.setItem('ferry-fx',manualOff?'off':'on')}catch{}sync();if(!manualOff)play()});
replay.addEventListener('click',()=>{cover.scrollIntoView({block:'start',behavior:'instant'});play()});motion.addEventListener('change',sync);sync();play();
(() => {
  const C=FerryCatalog, $=s=>document.querySelector(s), el=(name,value)=>{const n=document.createElement(name);n.textContent=value;return n};
  const snapshot=JSON.parse($('#ferry-snapshot').textContent);
  let pick='all'; const filters=$('#filters');filters.hidden=false;
  function filter(){let count=0;for(const row of document.querySelectorAll('#chain li')){row.hidden=pick!=='all'&&row.dataset.kind!==pick;if(!row.hidden)count++}$('#archive-empty').hidden=count>0;filters.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.pick===pick)))}
  filters.addEventListener('click',e=>{const b=e.target.closest('button');if(b){pick=b.dataset.pick;filter()}});
  function display(rows,notes){$('#release-head').innerHTML=C.head(rows);$('#chain').innerHTML=C.chain(rows,notes);$('#readme-notes').innerHTML=C.noteHTML(notes,rows.find(x=>x.kind==='app')?.tag);filter()}
  if(!/^https?:$/.test(location.protocol))return;
  const req=async(url,raw=false)=>{const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),12000);try{const res=await fetch(url,{credentials:'omit',signal:ctrl.signal,headers:{Accept:raw?'text/plain':'application/vnd.github+json'}});if(!res.ok)throw Error('HTTP '+res.status);return raw?await res.text():await res.json()}finally{clearTimeout(timer)}};
  async function releases(){let out=[];for(let page=1;;page++){const part=await req(`${C.api}/releases?per_page=100&page=${page}`);if(!Array.isArray(part))throw Error('invalid');out.push(...part);if(part.length<100)return C.normalize(out)}}
  async function refresh(){
    const [rs,ms]=await Promise.allSettled([releases(),req('https://raw.githubusercontent.com/RedRea1ity/ProjectFerry/main/README.md',true)]);
    const rows=rs.status==='fulfilled'?rs.value:snapshot.rows, notes=ms.status==='fulfilled'?C.readNotes(ms.value):snapshot.notes;
    display(rows,notes);$('#catalog-status').textContent=(rs.status==='fulfilled'?'已与 GitHub 发行目录同步':'发行目录暂未连通，显示 '+snapshot.when+' 随盘存档')+(ms.status==='fulfilled'?'':'；README 使用随盘摘录');
  }
  async function forum(){
    const state=$('#forum-state'),box=$('#threads');state.textContent='正在接收主题……';
    try{
      let topics=[];for(let page=1;topics.length<6;page++){const rows=await req(`${C.api}/issues?state=all&sort=updated&direction=desc&per_page=30&page=${page}`);if(!Array.isArray(rows))throw Error('issue list');topics.push(...rows.filter(x=>!x.pull_request&&Number.isSafeInteger(x.number)));if(rows.length<30)break}
      const frag=document.createDocumentFragment();for(const t of topics.slice(0,6)){
        const d=el('details',''),s=el('summary',''),num=el('span','#'+t.number),name=el('strong',t.title),meta=el('small',`${t.state==='closed'?'已结帖':'讨论中'} · ${t.comments} 回复`);s.append(num,name,meta);d.append(s);
        const by=el('small',`${t.user?.login||'GitHub 用户'}　/　${C.date(t.created_at)}`),p=el('p',(t.body||'（主题没有正文）').slice(0,450)),a=el('a','查看完整讨论及回复 ↗');a.href=`${C.repo}/issues/${t.number}`;const inner=el('div','');inner.className='thread-body';inner.append(by,p,a);d.append(inner);frag.append(d);
      }box.replaceChildren(frag);state.textContent=topics.length?'最近更新的主题（含已结帖）':'目前还没有主题，可以发第一帖。';
    }catch{state.textContent='暂时读不到帖子。可直接到 GitHub 查看全部讨论。'}
  }
  refresh().catch(()=>{$('#catalog-status').textContent='暂未完成同步，显示随盘存档'});
  if('IntersectionObserver' in window){const seen=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){seen.disconnect();forum()}},{rootMargin:'480px'});seen.observe($('#forum'))}else{forum()}
})();
