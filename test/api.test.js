import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';

test('password, validation, all ticket types, updates, comments and restart persistence',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'triage-test-'));let child;
 const start=async()=>{child=spawn(process.execPath,['server.js'],{env:{...process.env,PORT:'3199',DATA_DIR:dir,ACCESS_PASSWORD:'test-password',NODE_ENV:'production'},stdio:['ignore','pipe','pipe']});await Promise.race([once(child.stdout,'data'),once(child,'exit').then(()=>{throw Error('Server exited')})]);};
 const stop=async()=>{child.kill();await once(child,'exit')};
 const call=(path='',method='GET',data,auth=true)=>fetch('http://127.0.0.1:3199/api/tickets'+path,{method,headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer test-password'}:{})},body:data?JSON.stringify(data):undefined});
 try{await start();assert.equal((await call('','GET',null,false)).status,401);assert.equal((await call('','POST',{})).status,400);
 let first;
 for(const type of ['Issue','Request','Change']){const r=await call('','POST',{title:'Test '+type,description:'Details <script>alert(1)</script>',requester:'Sam',service:'Network',priority:'High',type});assert.equal(r.status,201);const t=await r.json();first??=t;}
 assert.equal((await(await call()).json()).length,3);
 assert.equal((await call('/'+first.id,'PATCH',{status:'Invalid',priority:'High'})).status,400);
 assert.equal((await call('/'+first.id,'PATCH',{status:'Resolved',priority:'Low'})).status,200);
 assert.equal((await call('/'+first.id+'/comments','POST',{author:'Sam',body:'Fixed the switch'})).status,201);
 assert.equal((await call('/9999')).status,404);
 const cross=await fetch('http://127.0.0.1:3199/api/tickets',{method:'POST',headers:{Authorization:'Bearer test-password',Origin:'https://evil.example'},body:'{}'});assert.equal(cross.status,403);
 await stop();await start();const saved=await(await call('/'+first.id)).json();assert.equal(saved.status,'Resolved');assert.equal(saved.priority,'Low');assert.equal(saved.comments[0].body,'Fixed the switch');assert.equal((await(await call()).json()).length,3);
 const page=await fetch('http://127.0.0.1:3199/');assert.equal(page.status,200);assert.match(await page.text(),/dark_triage/);assert.match(page.headers.get('content-security-policy'),/frame-ancestors 'none'/);
 }finally{if(child?.exitCode===null)await stop();rmSync(dir,{recursive:true,force:true});}
});
