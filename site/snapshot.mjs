import fs from 'node:fs';
import C from './src/catalog.cjs';
const get=async (url,raw=false)=>{const headers={'User-Agent':'ProjectFerry-Pages'};if(new URL(url).origin==='https://api.github.com'&&process.env.FERRY_GITHUB_TOKEN)headers.Authorization=`Bearer ${process.env.FERRY_GITHUB_TOKEN}`;const r=await fetch(url,{headers,signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error(`GitHub ${r.status}`);return raw? r.text():r.json()};
async function all(){const rows=[];for(let p=1;;p++){const rs=await get(`${C.api}/releases?per_page=100&page=${p}`);rows.push(...rs);if(rs.length<100)return rows}}
const [rows,md]=await Promise.all([all(),get('https://raw.githubusercontent.com/RedRea1ity/ProjectFerry/main/README.md',true)]);
const snapshot={when:C.date(new Date().toISOString()).slice(0,10),rows:C.normalize(rows),notes:C.readNotes(md)};
if(!snapshot.rows.length)throw Error('No published releases');fs.writeFileSync(new URL('./src/snapshot.json',import.meta.url),JSON.stringify(snapshot,null,2)+'\n');console.log('Source snapshot:',snapshot.rows.length,'releases;',Object.keys(snapshot.notes).length,'README records');
