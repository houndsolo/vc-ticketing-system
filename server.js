import http from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readFileSync } from 'node:fs';
import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';

mkdirSync(process.env.DATA_DIR || 'data', {recursive:true});
const db = new DatabaseSync(`${process.env.DATA_DIR || 'data'}/triage.db`);
db.exec(`PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL, type TEXT NOT NULL, priority TEXT NOT NULL, service TEXT NOT NULL, requester TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Open', created TEXT NOT NULL, updated TEXT NOT NULL); CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id), author TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL);`);
const types=['Issue','Request','Change'], priorities=['Low','Medium','High','Critical'], statuses=['Open','In progress','Waiting','Resolved'];
const password=process.env.ACCESS_PASSWORD;
if (!password && process.env.NODE_ENV==='production') throw new Error('Set ACCESS_PASSWORD before running in production.');
const json=(res,status,data)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data));};
const valid=(v,max)=>typeof v==='string' && v.trim().length>0 && v.length<=max;
async function body(req) {let raw='';for await(const chunk of req){raw+=chunk;if(raw.length>24000)throw Error('Request too large');}return JSON.parse(raw||'{}');}
export const server=http.createServer(async(req,res)=>{
 try {
 const url=new URL(req.url,'http://localhost');
 res.setHeader('X-Content-Type-Options','nosniff'); res.setHeader('Referrer-Policy','same-origin');
 res.setHeader('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
 if(url.pathname.startsWith('/api/')) {
   if(password){const token=req.headers.authorization?.replace(/^Bearer /,'')||'';const a=Buffer.from(token),b=Buffer.from(password);if(a.length!==b.length||!timingSafeEqual(a,b))return json(res,401,{error:'Enter the lab access password.'});}
   if(req.headers.origin && req.headers.origin!==`${req.headers['x-forwarded-proto']||'http'}://${req.headers.host}`) return json(res,403,{error:'Cross-origin request rejected'});
   if(url.pathname==='/api/tickets' && req.method==='GET')return json(res,200,db.prepare('SELECT * FROM tickets ORDER BY updated DESC, id DESC').all());
   if(url.pathname==='/api/tickets' && req.method==='POST'){
     const x=await body(req);if(!valid(x.title,160)||!valid(x.description,10000)||!valid(x.requester,80)||!valid(x.service,80)||!types.includes(x.type)||!priorities.includes(x.priority))return json(res,400,{error:'Complete all fields with valid values.'});
     const now=new Date().toISOString();const r=db.prepare('INSERT INTO tickets(title,description,type,priority,service,requester,created,updated) VALUES(?,?,?,?,?,?,?,?)').run(x.title.trim(),x.description.trim(),x.type,x.priority,x.service.trim(),x.requester.trim(),now,now);return json(res,201,db.prepare('SELECT * FROM tickets WHERE id=?').get(r.lastInsertRowid));
   }
   const match=url.pathname.match(/^\/api\/tickets\/(\d+)(\/comments)?$/);
   if(match){const id=Number(match[1]);const ticket=db.prepare('SELECT * FROM tickets WHERE id=?').get(id);if(!ticket)return json(res,404,{error:'Ticket not found'});
     if(req.method==='GET')return json(res,200,{...ticket,comments:db.prepare('SELECT * FROM comments WHERE ticket_id=? ORDER BY id').all(id)});
     if(req.method==='PATCH'&&!match[2]){const x=await body(req);if(!statuses.includes(x.status)||!priorities.includes(x.priority))return json(res,400,{error:'Invalid status or priority'});db.prepare('UPDATE tickets SET status=?,priority=?,updated=? WHERE id=?').run(x.status,x.priority,new Date().toISOString(),id);return json(res,200,{ok:true});}
     if(req.method==='POST'&&match[2]){const x=await body(req);if(!valid(x.author,80)||!valid(x.body,5000))return json(res,400,{error:'Enter your name and a comment.'});const now=new Date().toISOString();db.prepare('INSERT INTO comments(ticket_id,author,body,created) VALUES(?,?,?,?)').run(id,x.author.trim(),x.body.trim(),now);db.prepare('UPDATE tickets SET updated=? WHERE id=?').run(now,id);return json(res,201,{ok:true});}
   }return json(res,404,{error:'Endpoint not found'});
 }
 const files={'/':['index.html','text/html'],'/app.js':['app.js','text/javascript'],'/style.css':['style.css','text/css']};const file=files[url.pathname];if(!file){res.writeHead(404);return res.end('Not found');}res.writeHead(200,{'Content-Type':file[1]});res.end(readFileSync(fileURLToPath(new URL(`./public/${file[0]}`,import.meta.url))));
 }catch(e){json(res,400,{error:e.message==='Request too large'?'Request too large':'Unable to process request'});}
});
if(process.env.NODE_ENV!=='test')server.listen(Number(process.env.PORT||3000),'0.0.0.0',()=>console.log(`dark_triage listening on port ${process.env.PORT||3000}`));
