const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statuses=['Open','In progress','Waiting','Resolved'];
let tickets=[],users=[],assignable=[],assigneeFilter='',view='All tickets',filter='All',board=false,stat='',current=null,me=null,page='tickets',resetTarget=null;
const icons={Issue:'⊙',Request:'◇',Change:'⇄'};
const id=t=>`DT-${String(t.id).padStart(3,'0')}`;
const initials=name=>esc(String(name||'?').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'?');
const badge=t=>`<span class="badge status-${({Open:'open','In progress':'progress',Waiting:'waiting',Resolved:'resolved'})[t.status]}">${t.status==='Resolved'?'✓':'◉'} ${esc(t.status)}</span>`;
const when=iso=>iso?new Date(iso).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'Never';
// Assignee helpers: an avatar chip, and the <option>s for a picker.
const who=a=>a?esc(a.display_name)+(a.disabled?' (disabled)':''):'Unassigned';
const assigneeChip=(a,label=true)=>a?`<span class="requester assignee${a.disabled?' is-disabled':''}" title="Assigned to ${who(a)}"><span class="avatar">${initials(a.display_name)}</span>${label?who(a):''}</span>`:`<span class="requester assignee unassigned" title="Unassigned"><span class="avatar">–</span>${label?'Unassigned':''}</span>`;
function assigneeOptions(selected){
  const opts=assignable.map(u=>`<option value="${u.id}" ${u.id===selected?.id?'selected':''}>${esc(u.display_name)}${u.id===me?.id?' (me)':''}</option>`);
  // A disabled account keeps its tickets; show it so the picker reflects reality.
  if(selected&&!assignable.some(u=>u.id===selected.id))opts.unshift(`<option value="${selected.id}" selected>${who(selected)}</option>`);
  return `<option value="" ${selected?'':'selected'}>Unassigned</option>`+opts.join('');
}
function renderAssigneeFilter(){
  const sel=$('#assignee-filter'),known=new Map(assignable.map(u=>[String(u.id),u.display_name]));
  tickets.forEach(t=>t.assignee&&!known.has(String(t.assignee.id))&&known.set(String(t.assignee.id),who(t.assignee)));
  if(assigneeFilter&&!['me','none'].includes(assigneeFilter)&&!known.has(assigneeFilter))assigneeFilter='';
  sel.innerHTML=`<option value="">Anyone</option><option value="me">Assigned to me</option><option value="none">Unassigned</option>`+[...known].filter(([k])=>+k!==me?.id).map(([k,name])=>`<option value="${k}">${esc(name)}</option>`).join('');
  sel.value=assigneeFilter;
}
const assigneeMatch=t=>!assigneeFilter||(assigneeFilter==='none'?!t.assignee:assigneeFilter==='me'?t.assignee?.id===me?.id:String(t.assignee?.id)===assigneeFilter);
function toast(msg){$('#toast').textContent=msg;$('#toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').style.display='none',4000)}

// --- session ---------------------------------------------------------------
const TOKEN_KEY='dt-token';
function getToken(){try{return localStorage.getItem(TOKEN_KEY)||''}catch{return ''}}
function setToken(token){try{token?localStorage.setItem(TOKEN_KEY,token):localStorage.removeItem(TOKEN_KEY)}catch{}}
class AuthError extends Error{}
async function api(path,options={}){
  const res=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',Authorization:'Bearer '+getToken(),...options.headers}});
  let data={};try{data=await res.json()}catch{}
  if(res.status===401&&path!=='/login'){signedOut();throw new AuthError(data.error||'Sign in to continue.')}
  if(res.status===403&&data.code==='password_change_required'){showPassword(true);throw new AuthError(data.error)}
  if(!res.ok)throw Error(data.error||'Request failed');
  return data;
}
const report=err=>{if(!(err instanceof AuthError))toast(err.message)};
function applyMe(){
  const admin=me?.role==='admin';
  $('#me-avatar').innerHTML=me?initials(me.display_name):'DT';
  $('#me-name').textContent=me?me.display_name:'Not signed in';
  $('#me-role').textContent=me?`@${me.username} · ${admin?'Admin':'User'}`:'Sign in to continue';
  $('#header-user').textContent=me?`◈ ${me.display_name}`:'◈ Private lab';
  $$('.admin-only').forEach(el=>el.hidden=!admin);
  if(!admin&&page==='users')showPage('tickets');
}
function showLogin(){$$('dialog[open]').forEach(d=>d.close());if(!$('#login-dialog').open)$('#login-dialog').showModal();$('#login-username').focus()}
function signedOut(){setToken('');me=null;tickets=[];users=[];assignable=[];assigneeFilter='';applyMe();render();showLogin()}
async function logout(){try{await api('/logout',{method:'POST',body:'{}'})}catch{}signedOut()}
async function start(){
  if(!getToken())return signedOut();
  try{me=await api('/me');applyMe();if(me.must_change)showPassword(true);else await load()}catch(err){report(err)}
}
$('#login-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;$('#login-error').textContent='';
  try{const data=await api('/login',{method:'POST',body:JSON.stringify({username:$('#login-username').value,password:$('#login-password').value})});
    setToken(data.token);me=data.user;$('#login-dialog').close();$('#login-form').reset();applyMe();
    if(me.must_change)showPassword(true);else{await load();toast(`Welcome back, ${me.display_name}.`)}}
  catch(err){$('#login-error').textContent=err.message;$('#login-password').select()}finally{button.disabled=false}};
$('#login-dialog').addEventListener('cancel',e=>e.preventDefault());

// --- password change (forced or voluntary) ---------------------------------
function showPassword(forced){
  const dialog=$('#password-dialog');dialog.dataset.forced=forced?'1':'';
  $('#password-form').reset();$('#password-error').textContent='';$('#password-username').value=me?.username||'';
  $('#password-eyebrow').textContent=forced?'FIRST THINGS FIRST':'YOUR ACCOUNT';
  $('#password-title').innerHTML=(forced?'Set your password':'Change password')+'<span class="green">.</span>';
  $('#password-intro').textContent=forced?'You signed in with a temporary password. Choose your own (at least 8 characters) to open the workspace.':'Choose a new password of at least 8 characters. Your other devices will be signed out.';
  $('#current-password').previousElementSibling.textContent=forced?'Temporary password':'Current password';
  $('#password-close').hidden=$('#password-cancel').hidden=forced;$('#password-logout').hidden=!forced;
  $$('dialog[open]').forEach(d=>d!==dialog&&d.close());
  if(!dialog.open)dialog.showModal();$('#current-password').focus();
}
$('#password-dialog').addEventListener('cancel',e=>{if($('#password-dialog').dataset.forced)e.preventDefault()});
$('#password-logout').onclick=logout;
$('#password-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;
  if($('#new-password').value!==$('#confirm-password').value){$('#password-error').textContent='The new passwords don’t match.';return}
  button.disabled=true;$('#password-error').textContent='';
  try{const forced=$('#password-dialog').dataset.forced;const data=await api('/me/password',{method:'POST',body:JSON.stringify({current_password:$('#current-password').value,new_password:$('#new-password').value})});
    me=data.user;applyMe();$('#password-dialog').close();toast('Password saved.');if(forced)await load()}
  catch(err){if(!(err instanceof AuthError))$('#password-error').textContent=err.message}finally{button.disabled=false}};
$('#side-password').onclick=$('#header-password').onclick=()=>me&&showPassword(false);
$('#side-logout').onclick=$('#lock').onclick=logout;

// --- tickets -----------------------------------------------------------------
async function load(){try{[tickets,assignable]=await Promise.all([api('/tickets'),api('/users/assignable')]);renderAssigneeFilter();render()}catch(err){report(err)}}
function render(){const active=tickets.filter(t=>t.status!=='Resolved');$('#stat-active').textContent=active.length;$('#stat-progress').textContent=tickets.filter(t=>t.status==='In progress').length;$('#stat-critical').textContent=active.filter(t=>['Critical','High'].includes(t.priority)).length;$('#stat-resolved').textContent=tickets.filter(t=>t.status==='Resolved').length;$('#nav-count').textContent=active.length;
const q=$('#search').value.toLowerCase(),p=$('#priority-filter').value;
const items=tickets.filter(t=>(view==='All tickets'||(view==='Resolved'?t.status==='Resolved':t.type===view))&&(filter==='All'||t.status===filter)&&(!p||t.priority===p)&&assigneeMatch(t)&&(!q||[id(t),t.title,t.description,t.service,t.requester,t.assignee?.display_name].join(' ').toLowerCase().includes(q))&&(!stat||(stat==='active'&&t.status!=='Resolved')||(stat==='progress'&&t.status==='In progress')||(stat==='critical'&&t.status!=='Resolved'&&['High','Critical'].includes(t.priority))||(stat==='resolved'&&t.status==='Resolved')));
$('#queue-count').textContent=items.length;$('#result-count').textContent=me?`Showing ${items.length} of ${tickets.length} tickets`:'Sign in to see tickets';if(page==='tickets')$('#crumb').textContent=view==='All tickets'?view:view==='Resolved'?'Resolved':view+'s';$('#page-title').innerHTML=view==='All tickets'?'Order from chaos<span>.</span>':esc(view==='Resolved'?'Back to normal':view+'s')+'<span>.</span>';
if(!items.length){$('#tickets').innerHTML=`<div class="empty"><div class="empty-icon">◈</div><h3>${tickets.length?'Nothing on this radar.':'A quiet lab. A clean slate.'}</h3><p>${tickets.length?'Try another filter or search.':'Your issues, requests, and changes will find a home here.'}</p><button class="secondary" id="empty-action">${tickets.length?'Clear filters':'Create your first ticket ↗'}</button></div>`;$('#empty-action').onclick=tickets.length?reset:newTicket;return;}
if(board){$('#tickets').innerHTML='<div class="board">'+statuses.map(s=>`<section class="board-column"><h3>${s} <span class="muted">${items.filter(t=>t.status===s).length}</span></h3>${items.filter(t=>t.status===s).map(t=>`<button class="board-card" data-ticket="${t.id}"><span class="id">${id(t)}</span><strong>${esc(t.title)}</strong><div class="detail-meta"><span class="service">${esc(t.service)}</span><span class="priority ${t.priority}">● ${t.priority}</span>${assigneeChip(t.assignee,false)}</div></button>`).join('')}</section>`).join('')+'</div>';}else{$('#tickets').innerHTML=`<table><thead><tr><th>Ticket</th><th>Subject</th><th>Status</th><th class="hide-small">Priority</th><th class="hide-medium">Service</th><th class="hide-small">Requested by</th><th class="hide-small">Assignee</th></tr></thead><tbody>${items.map(t=>`<tr tabindex="0" role="button" aria-label="Open ${id(t)} ${esc(t.title)}" data-ticket="${t.id}"><td class="id">${id(t)}</td><td class="ticket-title"><span class="type-icon">${icons[t.type]}</span>${esc(t.title)}<small>${esc(t.type)} · ${new Date(t.created).toLocaleDateString(undefined,{month:'short',day:'numeric'})}</small></td><td>${badge(t)}</td><td class="hide-small"><span class="priority ${t.priority}">▥ ${t.priority}</span></td><td class="hide-medium"><span class="service">${esc(t.service)}</span></td><td class="hide-small"><span class="requester"><span class="avatar">${initials(t.requester)}</span>${esc(t.requester)}</span></td><td class="hide-small">${assigneeChip(t.assignee)}</td></tr>`).join('')}</tbody></table>`;}
$$('[data-ticket]').forEach(el=>{el.onclick=()=>openTicket(el.dataset.ticket);el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openTicket(el.dataset.ticket)}}});}
function reset(){view='All tickets';filter='All';stat='';assigneeFilter='';$('#search').value='';$('#priority-filter').value='';$('#assignee-filter').value='';$$('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$$('[data-filter]').forEach(b=>b.classList.toggle('selected',b.dataset.filter===filter));showPage('tickets')}
function newTicket(){if(!me)return showLogin();$('#ticket-form').reset();$('#ticket-as').textContent=me.display_name;$('#assignee').innerHTML=assigneeOptions(null);$('#ticket-dialog').showModal();$('#title').focus()}
$('#new-ticket').onclick=newTicket;$('#help-ticket').onclick=newTicket;
$$('.close').forEach(b=>b.onclick=()=>b.closest('dialog').close());
$$('[data-view]').forEach(b=>b.onclick=()=>{view=b.dataset.view;filter='All';stat='';$$('[data-view]').forEach(x=>x.classList.toggle('active',x===b));$$('[data-filter]').forEach(x=>x.classList.toggle('selected',x.dataset.filter==='All'));showPage('tickets')});
$$('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;stat='';$$('[data-filter]').forEach(x=>x.classList.toggle('selected',x===b));render()});
$$('[data-stat]').forEach(b=>b.onclick=()=>{reset();stat=b.dataset.stat;render()});
$('#search').oninput=render;$('#priority-filter').onchange=render;$('#assignee-filter').onchange=e=>{assigneeFilter=e.target.value;render()};$('#list-view').onclick=()=>{board=false;$('#list-view').classList.add('selected');$('#board-view').classList.remove('selected');render()};$('#board-view').onclick=()=>{board=true;$('#board-view').classList.add('selected');$('#list-view').classList.remove('selected');render()};
$('#ticket-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{const data=Object.fromEntries(new FormData(e.target));data.assignee_id=data.assignee_id?+data.assignee_id:null;const t=await api('/tickets',{method:'POST',body:JSON.stringify(data)});$('#ticket-dialog').close();reset();await load();toast(`${id(t)} created. It’s on the radar.`);await openTicket(t.id)}catch(err){report(err)}finally{button.disabled=false}};
async function openTicket(ticketId){try{current=await api('/tickets/'+ticketId);const t=current;$('#detail-content').innerHTML=`<div class="modal-heading"><div><div class="eyebrow">${id(t)} · ${esc(t.type)}</div><h2>${esc(t.title)}</h2></div><button class="icon-button" id="close-detail" aria-label="Close">✕</button></div><div class="detail-meta">${badge(t)}<span class="service">${esc(t.service)}</span><span class="muted">by ${esc(t.requester)}</span>${assigneeChip(t.assignee)}</div><p class="description">${esc(t.description)}</p><h3>Conversation <span class="muted">(${t.comments.filter(c=>c.kind!=='system').length})</span></h3>${t.comments.map(c=>c.kind==='system'?`<div class="comment activity"><span class="activity-mark">◈</span> <strong>${esc(c.author)}</strong> ${esc(c.body)} <small>${new Date(c.created).toLocaleString()}</small></div>`:`<div class="comment"><span class="requester"><span class="avatar">${initials(c.author)}</span><strong>${esc(c.author)}</strong></span> <small>${new Date(c.created).toLocaleString()}</small><p>${esc(c.body)}</p></div>`).join('')||'<p class="muted">No updates yet. Start the conversation.</p>'}<form id="update-form"><div class="form-grid"><div><label for="edit-status">Status</label><select id="edit-status" name="status">${statuses.map(s=>`<option ${s===t.status?'selected':''}>${s}</option>`).join('')}</select></div><div><label for="edit-priority">Priority</label><select id="edit-priority" name="priority">${['Low','Medium','High','Critical'].map(s=>`<option ${s===t.priority?'selected':''}>${s}</option>`).join('')}</select></div></div><label for="edit-assignee">Assignee</label><div class="inline-field"><select id="edit-assignee" name="assignee_id">${assigneeOptions(t.assignee)}</select><button type="button" class="secondary" id="assign-me">Assign to me <kbd>A</kbd></button></div><label for="comment">Add an update <span class="muted">as ${esc(me?.display_name)} · optional</span></label><textarea id="comment" name="body" maxlength="5000" rows="3" placeholder="A clue, a question, or a little progress…"></textarea><div class="modal-actions"><span class="muted form-note">Ctrl+Enter to save</span><button class="primary" type="submit">Save ↗</button></div></form>`;if(!$('#detail-dialog').open)$('#detail-dialog').showModal();$('#close-detail').onclick=()=>$('#detail-dialog').close();$('#update-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter||$('#update-form button[type=submit]');const data=Object.fromEntries(new FormData(e.target));const body=(data.body||'').trim();const changes={};if(data.status!==t.status)changes.status=data.status;if(data.priority!==t.priority)changes.priority=data.priority;const aid=data.assignee_id?+data.assignee_id:null;if(aid!==(t.assignee?.id??null))changes.assignee_id=aid;if(!Object.keys(changes).length&&!body)return toast('Nothing to save.');button.disabled=true;try{if(Object.keys(changes).length)await api('/tickets/'+t.id,{method:'PATCH',body:JSON.stringify(changes)});if(body)await api('/tickets/'+t.id+'/comments',{method:'POST',body:JSON.stringify({body})});await load();await openTicket(t.id);toast(Object.keys(changes).length&&body?'Saved and posted.':body?'Update posted.':'Ticket updated.')}catch(err){report(err);button.disabled=false}};$('#comment').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();$('#update-form').requestSubmit()}};$('#assign-me').onclick=assignToMe;}catch(err){report(err)}}

async function patchTicket(ticketId,changes,message){try{await api('/tickets/'+ticketId,{method:'PATCH',body:JSON.stringify(changes)});await load();await openTicket(ticketId);toast(message)}catch(err){report(err)}}
function assignToMe(){if(!current||!me)return;const sel=$('#edit-assignee');if(!sel)return;if(![...sel.options].some(o=>o.value==String(me.id)))return toast('You can’t be assigned.');sel.value=String(me.id);sel.focus();toast('Assignee set to you. Save to apply.')}

// --- users (admins only) -------------------------------------------------------
function showPage(name){
  page=name==='users'&&me?.role==='admin'?'users':'tickets';
  $('#tickets-page').hidden=page!=='tickets';$('#users-page').hidden=page!=='users';
  $('#nav-users').classList.toggle('active',page==='users');
  if(page==='users'){$$('[data-view]').forEach(b=>b.classList.remove('active'));$('#crumb').textContent='Users';loadUsers()}
  else{$$('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));render()}
}
$('#nav-users').onclick=()=>showPage('users');
async function loadUsers(){try{users=await api('/users');renderUsers()}catch(err){report(err)}}
function renderUsers(){
  $('#user-count').textContent=users.length;
  $('#users').innerHTML=`<table><thead><tr><th>User</th><th>Role</th><th>Status</th><th class="hide-small">Last sign-in</th><th>Actions</th></tr></thead><tbody>${users.map(u=>`<tr class="user-row${u.disabled?' is-disabled':''}"><td><span class="requester user-cell"><span class="avatar">${initials(u.display_name)}</span><span><strong>${esc(u.display_name)}</strong>${u.id===me.id?' <span class="you">you</span>':''}<small>@${esc(u.username)}</small></span></span></td><td><select data-role="${u.id}" aria-label="Role for ${esc(u.username)}"><option value="user" ${u.role==='user'?'selected':''}>User</option><option value="admin" ${u.role==='admin'?'selected':''}>Admin</option></select></td><td>${u.disabled?'<span class="badge status-disabled">⊘ Disabled</span>':u.must_change?'<span class="badge status-waiting">◌ Temporary password</span>':'<span class="badge status-resolved">✓ Active</span>'}</td><td class="hide-small muted">${esc(when(u.last_login))}</td><td><div class="row-actions"><button class="secondary small" data-reset="${u.id}">Reset password</button>${u.id===me.id?'':`<button class="secondary small${u.disabled?'':' danger'}" data-toggle="${u.id}">${u.disabled?'Enable':'Disable'}</button>`}</div></td></tr>`).join('')}</tbody></table>`;
  $$('[data-role]').forEach(sel=>sel.onchange=()=>updateUser(+sel.dataset.role,{role:sel.value},`Role changed to ${sel.value}.`));
  $$('[data-toggle]').forEach(b=>b.onclick=()=>{const u=users.find(x=>x.id===+b.dataset.toggle);if(!u.disabled&&!confirm(`Disable ${u.display_name}? They’ll be signed out and can’t sign in until re-enabled.`))return;updateUser(u.id,{disabled:!u.disabled},u.disabled?`${u.display_name} can sign in again.`:`${u.display_name} is disabled and signed out.`)});
  $$('[data-reset]').forEach(b=>b.onclick=()=>{resetTarget=users.find(x=>x.id===+b.dataset.reset);$('#reset-form').reset();$('#reset-error').textContent='';$('#reset-title').innerHTML=`Reset ${esc(resetTarget.display_name)}<span class="green">.</span>`;$('#reset-dialog').showModal();$('#reset-password').focus()});
}
async function updateUser(userId,changes,message){
  try{await api('/users/'+userId,{method:'PATCH',body:JSON.stringify(changes)});toast(message);if(userId===me.id){me=await api('/me');applyMe()}}catch(err){report(err)}
  if(page==='users')await loadUsers();
}
function newUser(){$('#user-form').reset();$('#user-error').textContent='';$('#user-dialog').showModal();$('#user-username').focus()}
$('#new-user').onclick=newUser;
function generatePassword(){const words='abcdefghjkmnpqrstuvwxyz23456789',bytes=crypto.getRandomValues(new Uint8Array(15));return [...bytes].map(b=>words[b%words.length]).join('').replace(/(.{5})(?!$)/g,'$1-')}
$$('.generate').forEach(b=>b.onclick=()=>{const input=$('#'+b.dataset.target);input.value=generatePassword();input.select()});
$('#user-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;$('#user-error').textContent='';
  try{const data=Object.fromEntries(new FormData(e.target));const u=await api('/users',{method:'POST',body:JSON.stringify(data)});$('#user-dialog').close();toast(`Account @${u.username} created. Share the temporary password privately.`);await loadUsers()}
  catch(err){if(!(err instanceof AuthError))$('#user-error').textContent=err.message}finally{button.disabled=false}};
$('#reset-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;$('#reset-error').textContent='';
  try{await api(`/users/${resetTarget.id}/password`,{method:'POST',body:JSON.stringify({password:$('#reset-password').value})});$('#reset-dialog').close();
    if(resetTarget.id===me.id)return signedOut();toast(`${resetTarget.display_name} must set a new password at next sign-in.`);await loadUsers()}
  catch(err){if(!(err instanceof AuthError))$('#reset-error').textContent=err.message}finally{button.disabled=false}};

// --- keyboard ---------------------------------------------------------------------
document.addEventListener('keydown',e=>{if(!me||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)||e.ctrlKey||e.metaKey||e.altKey)return;
  const open=$$('dialog[open]');if(open.length){if(e.key==='a'&&open.length===1&&open[0].id==='detail-dialog'){e.preventDefault();assignToMe()}return}if(e.key==='n'){e.preventDefault();page==='users'?newUser():newTicket()}if(e.key==='/'&&page==='tickets'){e.preventDefault();$('#search').focus()}});
start();
