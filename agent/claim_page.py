"""claim_page.py - the page where a project's first administrator claims their invitation (v15).

One function, render(), returns (html, content_security_policy). console_auth.py serves it at /auth/claim.

Steps: 1 invitation code + choose a username and password  ->  2 enter the 6-digit code e-mailed to the invited
address  ->  3 done. Same look and the same strict security policy as the sign-in page (one nonce-protected style
and script, no inline handlers, no external files). The page never shows the invited person's address until the
invitation code has been accepted.
"""
from __future__ import annotations

import html as _html
import json
import secrets

MARK = ('<svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true"><rect width="32" height="32" rx="6" fill="#86BC25"/>'
        '<path d="M8.5 21 13.5 16l-5-5M17 22h7" fill="none" stroke="#000" stroke-width="2.6" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg>')

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Set up your console - AI Delivery Console</title>
<style nonce="__NONCE__">
:root{--bg:#f2f2f2;--card:#fff;--ink:#1b1b1b;--muted:#605e5c;--line:#e1dfdd;--field:#8a8886;--field-h:#323130;
--primary:#111;--primary-h:#333;--accent:#86bc25;--link:#2d6a0f;--err:#a4262c;--err-bg:#fde7e9;--info-bg:#eff6fc;--info-ink:#004578;
--ok-bg:#dff6dd;--ok-ink:#107c10;--warn-bg:#fff4ce;--warn-ink:#5c4400;
font-family:"Segoe UI","Segoe UI Web (West European)",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.45;display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:32px 16px}
body::before,body::after{content:"";flex:1 0 0}
.card{width:100%;max-width:460px;background:var(--card);border:1px solid var(--line);border-radius:6px;box-shadow:0 2px 6px rgba(0,0,0,.06);padding:40px 44px 36px}
.brand{display:flex;align-items:center;gap:10px;margin:0 0 6px}.brand span{font-size:15px;font-weight:600}
.project{margin:0 0 24px;color:var(--muted);font-size:13.5px}
.step{margin:0 0 6px;font-size:12.5px;color:var(--muted)}
h1{margin:0 0 6px;font-size:24px;line-height:1.25;font-weight:600}h1:focus{outline:none}
.sub{margin:0 0 18px;color:var(--muted);font-size:14px}
label{display:block;margin:14px 0 5px;font-size:14px;font-weight:600}
.field{position:relative}
input{width:100%;font:inherit;font-size:15px;color:var(--ink);background:var(--card);border:1px solid var(--field);border-radius:4px;padding:8px 11px;min-height:38px}
input:hover{border-color:var(--field-h)}input:focus{outline:none;border-color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink)}
input.bad{border-color:var(--err);box-shadow:inset 0 0 0 1px var(--err)}
.field input.pw{padding-right:64px}
.eye{position:absolute;right:3px;top:3px;height:32px;padding:0 9px;border:0;background:none;color:var(--muted);border-radius:3px;cursor:pointer;font-size:12.5px}
.hint{margin:5px 0 0;font-size:12.5px;color:var(--muted)}
.token{font-family:Consolas,"Cascadia Mono",monospace;letter-spacing:.12em;text-transform:uppercase}
.code{font-size:22px;letter-spacing:.42em;text-align:center;font-variant-numeric:tabular-nums;padding-left:.42em}
.actions{margin-top:24px}
.go{width:100%;min-height:40px;display:flex;align-items:center;justify-content:center;gap:9px;font:inherit;font-size:15px;font-weight:600;color:#fff;background:var(--primary);border:1px solid var(--primary);border-radius:4px;cursor:pointer}
.go:hover{background:var(--primary-h)}.go:disabled{opacity:.5;cursor:default}
.go:focus-visible,.lnk:focus-visible,.eye:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.spin{display:none;width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:turn .8s linear infinite}
.go.loading .spin{display:inline-block}@keyframes turn{to{transform:rotate(360deg)}}
.links{margin-top:18px;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:14px}
.lnk{padding:0;font:inherit;font-size:14px;color:var(--link);background:none;border:0;cursor:pointer;text-decoration:none}
.lnk:hover{text-decoration:underline}.lnk:disabled{color:var(--muted);cursor:default;text-decoration:none}
.msg{margin:0 0 14px;padding:9px 12px;border-radius:4px;font-size:13.5px;line-height:1.4}.msg:empty{display:none}
.msg.err{background:var(--err-bg);color:var(--err)}.msg.info{background:var(--info-bg);color:var(--info-ink)}.msg.ok{background:var(--ok-bg);color:var(--ok-ink)}
.meta{margin:8px 0 0;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.done{width:44px;height:44px;border-radius:50%;background:var(--ok-bg);color:var(--ok-ink);display:grid;place-items:center;margin:0 0 14px}
.done svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.notice{margin:18px 0 0;max-width:460px;text-align:center;font-size:12.5px;color:var(--muted)}
@media (max-width:520px){body{padding:0;justify-content:flex-start;background:var(--card)}.card{border:0;box-shadow:none;border-radius:0;padding:28px 22px}.notice{padding:0 22px 24px}}
@media (prefers-color-scheme:dark){
 :root{--bg:#141414;--card:#1f1f1f;--ink:#f3f2f1;--muted:#b3b0ad;--line:#2f2f2f;--field:#6e6e6e;--field-h:#c8c6c4;--primary:#f3f2f1;--primary-h:#d6d6d6;
 --link:#9fd356;--err:#f1707b;--err-bg:#3b1a1d;--info-bg:#152b3d;--info-ink:#a6d1f5;--ok-bg:#16300f;--ok-ink:#9ad48a}
 .go{color:#111}.spin{border-color:rgba(0,0,0,.25);border-top-color:#111}
}
</style></head><body>
<main class="card">
 <div class="brand">__MARK__<span>AI Delivery Console</span></div>
 <p class="project">__PROJECT__</p>
 <section class="view" id="vClosed" __CLOSED__>
  <h1 tabindex="-1">Nothing to set up</h1>
  <p class="sub">There is no open invitation for this console. If you were invited, the invitation may have expired or already been used. Ask the platform team to send a new one.</p>
  <div class="links"><a class="lnk" id="toSign" href="/auth/signin">Go to sign in</a></div>
 </section>
 <section class="view" id="vStart" __OPEN__ aria-labelledby="hStart">
  <p class="step">Step 1 of 2</p>
  <h1 id="hStart" tabindex="-1">Set up your console</h1>
  <p class="sub">__LEAD__ Enter the invitation code from the platform team's e-mail, then choose how you will sign in. We will send a code to the invited address to confirm it is you.</p>
  <div class="msg" role="status" aria-live="polite"></div>
  <form id="fStart" novalidate autocomplete="off">
   <label for="tk">Invitation code</label><div class="field"><input class="token" id="tk" name="token" maxlength="40" spellcheck="false" autocomplete="off" autocapitalize="characters"></div>
   <label for="nm">Your full name</label><div class="field"><input id="nm" name="name" autocomplete="name" maxlength="80"></div>
   <label for="un">Username</label><div class="field"><input id="un" name="username" autocomplete="username" maxlength="32" spellcheck="false" autocapitalize="none"></div>
   <label for="pw">Password</label>
   <div class="field"><input class="pw" id="pw" name="password" type="password" autocomplete="new-password" maxlength="128"><button class="eye" type="button" id="eye" aria-pressed="false" aria-label="Show password">Show</button></div>
   <p class="hint">At least __MINPW__ characters. Don't include your username.</p>
   <label for="pw2">Confirm password</label><div class="field"><input class="pw" id="pw2" type="password" autocomplete="new-password" maxlength="128"></div>
   <div class="actions"><button class="go" id="goStart" type="submit"><span class="lbl">Continue</span><span class="spin" aria-hidden="true"></span></button></div>
  </form>
 </section>
 <section class="view" id="vCode" hidden aria-labelledby="hCode">
  <p class="step">Step 2 of 2</p>
  <h1 id="hCode" tabindex="-1">Verify your email</h1>
  <p class="sub">Enter the 6-digit code we sent to <b id="sentTo"></b>.</p>
  <div class="msg" role="status" aria-live="polite"></div>
  <form id="fCode" novalidate autocomplete="off">
   <label for="rc">Verification code</label>
   <div class="field"><input class="code" id="rc" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}"></div>
   <p class="meta" id="expiry" aria-live="off"></p>
   <div class="actions"><button class="go" id="goCode" type="submit"><span class="lbl">Verify and finish</span><span class="spin" aria-hidden="true"></span></button></div>
  </form>
  <div class="links"><button class="lnk" id="resend" type="button" disabled>Send a new code</button><button class="lnk" id="back" type="button">Change details</button></div>
 </section>
 <section class="view" id="vDone" hidden aria-labelledby="hDone">
  <div class="done"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg></div>
  <h1 id="hDone" tabindex="-1">Your console is ready</h1>
  <p class="sub">You are the project administrator. Sign in with your username and password, then review access requests and manage your team from the console.</p>
  <div class="actions"><a class="go" id="goSign" href="/auth/signin">Go to sign in</a></div>
 </section>
</main>
<p class="notice">Authorized users only. Activity on this console is logged.</p>
<script nonce="__NONCE__">
(function(){
'use strict';
const MINPW=__MINPW__; const $=id=>document.getElementById(id);
const NETERR='The console service is not responding. Check that it is running, then try again.';
let current=$('vClosed')&&!$('vClosed').hidden?'vClosed':'vStart', vid='', expiresAt=0, resendAt=0, timer=null;
const USER=/^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/;
function msgEl(){return $(current).querySelector('.msg');}
function say(t,k){const m=msgEl();if(!m)return;m.textContent=t||'';m.className='msg'+(k?' '+k:'');m.setAttribute('role',k==='err'?'alert':'status');}
function view(n){for(const id of ['vClosed','vStart','vCode','vDone']){const e=$(id);if(e)e.hidden=(id!==n);}current=n;say('');
  const h=$(n).querySelector('h1');if(h)h.focus({preventScroll:true});const f=$(n).querySelector('input');if(f&&n!=='vDone')f.focus({preventScroll:true});}
function busy(b,on,txt,orig){b.classList.toggle('loading',on);b.disabled=on;b.querySelector('.lbl').textContent=on&&txt?txt:orig;}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify(body||{})});
  return [r.status,await r.json().catch(()=>({}))];}
function mmss(s){s=Math.max(0,Math.round(s));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}
function tick(){const now=Date.now(),left=(expiresAt-now)/1000,wait=(resendAt-now)/1000;
  $('expiry').textContent=left>0?'The code expires in '+mmss(left)+'.':'The code has expired. Send a new code.';
  $('resend').disabled=wait>0;$('resend').textContent=wait>0?'Send a new code ('+Math.ceil(wait)+'s)':'Send a new code';
  $('goCode').disabled=left<=0||$('goCode').classList.contains('loading');}
function arm(j){vid=j.id||vid;expiresAt=Date.now()+j.expires_in_s*1000;resendAt=Date.now()+j.resend_in_s*1000;$('sentTo').textContent=j.email||'';clearInterval(timer);tick();timer=setInterval(tick,1000);}
const hp=new URLSearchParams(location.hash.slice(1));
if(hp.get('t'))sessionStorage.setItem('rcTok',hp.get('t'));          // lets the sign-in page open without the long address
const tokenFromHash=hp.get('c');
if($('tk')&&tokenFromHash){$('tk').value=tokenFromHash;}
if(location.hash)history.replaceState(null,'',location.pathname);   // never leave codes or tokens in the address bar
$('eye')&&$('eye').addEventListener('click',()=>{const on=$('eye').getAttribute('aria-pressed')!=='true';$('eye').setAttribute('aria-pressed',on?'true':'false');
  $('eye').textContent=on?'Hide':'Show';$('eye').setAttribute('aria-label',on?'Hide password':'Show password');$('pw').type=on?'text':'password';$('pw2').type=on?'text':'password';});
$('fStart')&&$('fStart').addEventListener('submit',async e=>{
  e.preventDefault();['tk','nm','un','pw','pw2'].forEach(i=>$(i).classList.remove('bad'));
  const bad=(i,t)=>{$(i).classList.add('bad');$(i).focus();say(t,'err');};
  if(!$('tk').value.trim())return bad('tk','Enter the invitation code from the e-mail.');
  if($('nm').value.trim().length<2)return bad('nm','Enter your full name.');
  if(!USER.test($('un').value.trim()))return bad('un','Username: 3\u201332 letters, digits, dot, dash or underscore, starting with a letter or digit.');
  if($('pw').value.length<MINPW)return bad('pw','Use at least '+MINPW+' characters for the password.');
  if($('pw').value!==$('pw2').value)return bad('pw2','The passwords do not match.');
  const b=$('goStart');busy(b,true,'Checking\u2026','Continue');say('');
  try{const [st,j]=await post('/auth/claim/start',{token:$('tk').value,name:$('nm').value,username:$('un').value.trim(),password:$('pw').value});
    if(st===202){$('pw').value='';$('pw2').value='';$('tk').value='';$('rc').value='';arm(j);busy(b,false,'','Continue');view('vCode');return;}
    say(j.message||('Could not continue ('+st+').'),'err');
    if(j.error==='invalid_invitation')$('tk').classList.add('bad');if(j.error==='username_taken'||j.error==='bad_username')$('un').classList.add('bad');
    if(j.error==='weak_password')$('pw').classList.add('bad');
  }catch(err){say(NETERR,'err');}
  busy(b,false,'','Continue');});
$('rc')&&$('rc').addEventListener('input',()=>{const v=$('rc').value.replace(/\D/g,'').slice(0,6);if(v!==$('rc').value)$('rc').value=v;$('rc').classList.remove('bad');});
$('fCode')&&$('fCode').addEventListener('submit',async e=>{
  e.preventDefault();if(!/^\d{6}$/.test($('rc').value)){$('rc').classList.add('bad');$('rc').focus();say('Enter the 6-digit code from the email.','err');return;}
  const b=$('goCode');busy(b,true,'Verifying\u2026','Verify and finish');say('');
  try{const [st,j]=await post('/auth/claim/verify',{id:vid,code:$('rc').value});
    if(st===201){clearInterval(timer);busy(b,false,'','Verify and finish');view('vDone');return;}
    say(j.message||('Could not verify the code ('+st+').'),'err');$('rc').classList.add('bad');$('rc').select();
    if(j.error==='expired')expiresAt=Date.now();
    if(['not_found','too_many_attempts','invitation_closed'].includes(j.error)){clearInterval(timer);$('goCode').disabled=true;$('resend').disabled=true;}
  }catch(err){say(NETERR,'err');}
  busy(b,false,'','Verify and finish');tick();});
$('resend')&&$('resend').addEventListener('click',async()=>{$('resend').disabled=true;say('');
  try{const [st,j]=await post('/auth/claim/resend',{id:vid});
    if(st===200){arm(j);$('rc').value='';$('rc').focus();say('A new code has been sent. Earlier codes no longer work.','info');return;}
    if(j.resend_in_s!==undefined)resendAt=Date.now()+j.resend_in_s*1000;say(j.message||('Could not send a new code ('+st+').'),'err');
  }catch(err){say(NETERR,'err');}tick();});
$('back')&&$('back').addEventListener('click',()=>{clearInterval(timer);vid='';view('vStart');});
const f0=$('tk')||$('toSign');if(f0)f0.focus();
})();
</script></body></html>"""


def render(project_name: str = "", is_open: bool = False, admin_hint: str = "", min_password: int = 12,
           nonce: str | None = None) -> tuple[str, str]:
    nonce = nonce or secrets.token_urlsafe(16)
    project = _html.escape(project_name) if project_name else "&nbsp;"
    lead = _html.escape(admin_hint) + " " if admin_hint else ""
    page = PAGE
    for key, val in (("__MARK__", MARK), ("__PROJECT__", project), ("__LEAD__", lead),
                     ("__OPEN__", "" if is_open else "hidden"), ("__CLOSED__", "hidden" if is_open else ""),
                     ("__MINPW__", str(int(min_password))), ("__NONCE__", nonce)):
        page = page.replace(key, val)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    return page, csp
