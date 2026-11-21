import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import C from './src/catalog.cjs';
const root=path.dirname(fileURLToPath(import.meta.url));
const read=file=>fs.readFileSync(path.join(root,file),'utf8');
const inline=(file,mime)=>`data:${mime};base64,${fs.readFileSync(path.join(root,'public',file)).toString('base64')}`;
const notes=`ProjectFerry icon/workbench: Copyright (c) 2026 摆渡计划 / ProjectFerry contributors.\n${read('project-license.txt')}\nNo third-party runtime, typeface or tracking script is fetched.`;
const snapshot=JSON.parse(read('src/snapshot.json'));
const current=snapshot.rows.find(x=>x.kind==='app');if(!current||!snapshot.notes[current.tag])console.warn('No matching README notes or desktop release.');
const css=read('src/depot.css').replace("url('../public/sleeve.webp')",'none')+'\n'+read('src/dispatch.css')+'\n'+read('src/gallery.css');
const html=read('index.html').replace('<!-- release-head -->',()=>C.head(snapshot.rows)).replace('<!-- release-chain -->',()=>C.chain(snapshot.rows,snapshot.notes)).replace('<!-- readme-notes -->',()=>C.noteHTML(snapshot.notes,current?.tag)).replace('<!-- snapshot-date -->',()=>C.esc(snapshot.when)).replace('<link rel="stylesheet" href="./src/depot.css">',()=>`<style>${css}</style>`).replace('./ferry-icon.png',()=>inline('ferry-icon.png','image/png')).replace('./workbench.png',()=>inline('workbench.png','image/png')).replace('<script type="module" src="./src/depot.js"></script>',()=>`<!-- ${notes} -->\n<script id="ferry-snapshot" type="application/json">${JSON.stringify(snapshot).replaceAll('<','\\u003c')}</script>\n<script>${read('src/catalog.cjs')}\n${read('src/depot.js')}\n${read('src/player.js')}</script>`);
const page=html.replace(/src="\.\/((?:ferry-(?:(?:portrait|last-boat)-l10n|chibi-disc)|bonus-(?:point|phone))\.webp)"/g,(_,file)=>`src="${inline(file,'image/webp')}"`);
const dest=process.env.FERRY_SITE_OUT?path.resolve(process.env.FERRY_SITE_OUT):path.resolve(root,'../../outputs/ferry-y2k');fs.mkdirSync(dest,{recursive:true});fs.writeFileSync(path.join(dest,'index.html'),page);console.log(`${snapshot.rows.length} releases; primary: ${current?.tag}; ${Buffer.byteLength(page)} bytes`);
fs.mkdirSync(path.join(dest,'media'),{recursive:true});
for(const track of ['weather-night.mp3','bonus-jiese.mp3']){
  fs.copyFileSync(path.join(root,'public','media',track),path.join(dest,'media',track));
}
