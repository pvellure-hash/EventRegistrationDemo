"""signin_page.py - the console's sign-in page (v14).

One function, render(), returns (html, content_security_policy). console_auth.py calls it for /auth/signin.

Design: a single centred card in the style of established identity pages - product mark, one heading, the fields,
one primary button, one secondary link - and a one-line notice under the card. Nothing else.

Views (accounts mode):  Sign in  |  Request access, step 1 (details)  |  step 2 (emailed code)  |  Request sent
Other modes show one button (Windows Security prompt, Microsoft sign-in, or this Windows user).

No external files: inline SVG, system fonts. Strict CSP (a nonce for the one style and one script block, no inline
style attributes or event handlers). Keyboard and screen-reader friendly, follows the Windows light/dark setting,
collapses to full width on small windows.
"""
from __future__ import annotations

import html as _html
import json
import secrets

MODES = ("entra", "windows", "accounts", "local")

MARK = ('<svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true"><rect width="32" height="32" rx="6" fill="#86BC25"/>'
        '<path d="M8.5 21 13.5 16l-5-5M17 22h7" fill="none" stroke="#000" stroke-width="2.6" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg>')
ICON_WINDOWS = ('<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" '
                'd="M3 5.6 10.5 4.5v7H3zM11.6 4.3 21 3v8.5h-9.4zM3 12.5h7.5v7L3 18.4zM11.6 12.5H21V21l-9.4-1.3z"/></svg>')
ICON_MICROSOFT = ('<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="#f25022" d="M3 3h8.5v8.5H3z"/>'
                  '<path fill="#7fba00" d="M12.5 3H21v8.5h-8.5z"/><path fill="#00a4ef" d="M3 12.5h8.5V21H3z"/>'
                  '<path fill="#ffb900" d="M12.5 12.5H21V21h-8.5z"/></svg>')
ICON_EYE = ('<svg class="i-show" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/>'
            '<circle cx="12" cy="12" r="3"/></svg>'
            '<svg class="i-hide" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18M10.6 5.2A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.2M6.6 6.7C3.7 8.6 2 12 2 12s3.6 7 10 7a10 10 0 0 0 4.2-.9"/>'
            '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>')

COPY = {
    "accounts": ("Sign in", "", "Sign in", ""),
    "windows": ("Sign in", "Use your Windows account. Windows will ask for your PIN, fingerprint, face or password.",
                "Sign in with Windows", ICON_WINDOWS),
    "entra": ("Sign in", "Use your organization account.", "Sign in with Microsoft", ICON_MICROSOFT),
    "local": ("Sign in", "Single-user mode: you will be signed in as the Windows user running this console.",
              "Continue", ""),
}

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Sign in - AI Delivery Console</title>
<style nonce="__NONCE__">
:root{--bg:#f2f2f2;--card:#fff;--ink:#1b1b1b;--muted:#605e5c;--line:#e1dfdd;--field:#8a8886;--field-h:#323130;
--primary:#111;--primary-h:#333;--accent:#86bc25;--link:#2d6a0f;--err:#a4262c;--err-bg:#fde7e9;--info-bg:#eff6fc;--info-ink:#004578;
--ok-bg:#dff6dd;--ok-ink:#107c10;--warn-bg:#fff4ce;--warn-ink:#5c4400;
font-family:"Segoe UI","Segoe UI Web (West European)",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.45;display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:32px 16px}
body::before,body::after{content:"";flex:1 0 0}
.card{width:100%;max-width:440px;background:var(--card);border:1px solid var(--line);border-radius:6px;box-shadow:0 2px 6px rgba(0,0,0,.06);padding:40px 44px 36px}
.brand{display:flex;align-items:center;gap:10px;margin:0 0 26px}
.brand span{font-size:15px;font-weight:600;letter-spacing:.005em}
.view[hidden]{display:none}
.step{margin:0 0 6px;font-size:12.5px;color:var(--muted)}
h1{margin:0 0 6px;font-size:24px;line-height:1.25;font-weight:600}
h1:focus{outline:none}
.sub{margin:0 0 18px;color:var(--muted);font-size:14px}
.sub:empty{display:none}
label{display:block;margin:14px 0 5px;font-size:14px;font-weight:600}
.opt{font-weight:400;color:var(--muted)}
.field{position:relative}
input,textarea{width:100%;font:inherit;font-size:15px;color:var(--ink);background:var(--card);border:1px solid var(--field);border-radius:4px;padding:8px 11px;min-height:38px}
textarea{resize:vertical;min-height:60px}
input:hover,textarea:hover{border-color:var(--field-h)}
input:focus,textarea:focus{outline:none;border-color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink)}
input.bad{border-color:var(--err);box-shadow:inset 0 0 0 1px var(--err)}
.field input.pw{padding-right:42px}
.eye{position:absolute;right:3px;top:3px;width:32px;height:32px;border:0;background:none;color:var(--muted);border-radius:3px;cursor:pointer;display:grid;place-items:center}
.eye:hover{color:var(--ink);background:rgba(0,0,0,.05)}
.eye .i-hide{display:none}.eye[aria-pressed=true] .i-show{display:none}.eye[aria-pressed=true] .i-hide{display:block}
.hint{margin:5px 0 0;font-size:12.5px;color:var(--muted)}
.caps{margin:5px 0 0;font-size:12.5px;color:var(--warn-ink);font-weight:600}
.caps[hidden]{display:none}
.code{font-size:22px;letter-spacing:.42em;text-align:center;font-variant-numeric:tabular-nums;padding-left:.42em}
.actions{margin-top:24px}
.go{width:100%;min-height:40px;display:flex;align-items:center;justify-content:center;gap:9px;font:inherit;font-size:15px;font-weight:600;color:#fff;background:var(--primary);border:1px solid var(--primary);border-radius:4px;cursor:pointer}
.go:hover{background:var(--primary-h)}
.go.alt{background:var(--card);color:var(--ink);border-color:var(--field)}
.go.alt:hover{background:rgba(0,0,0,.04)}
.go:disabled{opacity:.5;cursor:default}
.go:focus-visible,.lnk:focus-visible,.eye:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.spin{display:none;width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:turn .8s linear infinite}
.go.alt .spin{border-color:rgba(0,0,0,.2);border-top-color:var(--ink)}
.go.loading .spin{display:inline-block}.go.loading .ico{display:none}
.go .ico{display:grid;place-items:center}
@keyframes turn{to{transform:rotate(360deg)}}
.links{margin-top:18px;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:14px}
.lnk{padding:0;font:inherit;font-size:14px;color:var(--link);background:none;border:0;cursor:pointer;text-decoration:none}
.lnk:hover{text-decoration:underline}
.lnk:disabled{color:var(--muted);cursor:default;text-decoration:none}
.msg{margin:0 0 14px;padding:9px 12px;border-radius:4px;font-size:13.5px;line-height:1.4}
.msg:empty{display:none}
.msg.err{background:var(--err-bg);color:var(--err)}
.msg.warn{background:var(--warn-bg);color:var(--warn-ink)}
.msg.info,.msg.busy{background:var(--info-bg);color:var(--info-ink)}
.msg.ok{background:var(--ok-bg);color:var(--ok-ink)}
.meta{margin:8px 0 0;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.done{width:44px;height:44px;border-radius:50%;background:var(--ok-bg);color:var(--ok-ink);display:grid;place-items:center;margin:0 0 14px}
.done svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.notice{margin:18px 0 0;max-width:440px;text-align:center;font-size:12.5px;color:var(--muted)}
@media (max-width:520px){body{padding:0;justify-content:flex-start;background:var(--card)}.card{border:0;box-shadow:none;border-radius:0;padding:28px 22px}.notice{padding:0 22px 24px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (prefers-color-scheme:dark){
 :root{--bg:#141414;--card:#1f1f1f;--ink:#f3f2f1;--muted:#b3b0ad;--line:#2f2f2f;--field:#6e6e6e;--field-h:#c8c6c4;
 --primary:#f3f2f1;--primary-h:#d6d6d6;--link:#9fd356;--err:#f1707b;--err-bg:#3b1a1d;--info-bg:#152b3d;--info-ink:#a6d1f5;
 --ok-bg:#16300f;--ok-ink:#9ad48a;--warn-bg:#3a300c;--warn-ink:#f4d980}
 .go{color:#111}.spin{border-color:rgba(0,0,0,.25);border-top-color:#111}.eye:hover{background:rgba(255,255,255,.07)}
 .go.alt:hover{background:rgba(255,255,255,.06)}
}
</style></head><body>
<main class="card">
 <div class="brand">__MARK__<span>AI Delivery Console</span></div>
 <section class="view" id="vSign" aria-labelledby="hSign">
  <h1 id="hSign" tabindex="-1">__TITLE__</h1>
  <p class="sub">__NOTE__</p>
  <div class="msg" role="status" aria-live="polite"></div>
  <form id="fSign" novalidate autocomplete="on">__FIELDS__
   <div class="actions"><button class="go" id="go" type="submit"><span class="ico">__ICON__</span><span class="lbl">__BUTTON__</span><span class="spin" aria-hidden="true"></span></button></div>
  </form>
  __REGLINK__
 </section>
 __REGVIEWS__
</main>
<p class="notice">Authorized users only. Activity on this console is logged.</p>
<script nonce="__NONCE__">
(function(){
'use strict';
const next=__NEXT__; const MODE=__MODE__; const REG=__REG__; const MINPW=__MINPW__; const MINUTES=__MINUTES__;
const TOKEN=new URLSearchParams(location.hash.slice(1)).get('t')||sessionStorage.getItem('rcTok')||'';
if(TOKEN)sessionStorage.setItem('rcTok',TOKEN);
const H={'X-Console-Token':TOKEN,'Content-Type':'application/json'};
const $=id=>document.getElementById(id);
const go=$('go'); const LBL=go.querySelector('.lbl'); const LBL0=LBL.textContent;
const NOTOKEN='Open the console from its desktop shortcut or from the address printed by run_console.py.';
const NETERR='The console service is not responding. Check that it is running, then try again.';
let busy=false, locked=false, lockTimer=null, current='vSign';

function msgEl(){return $(current).querySelector('.msg');}
function say(text,kind){const m=msgEl();m.textContent=text||'';m.className='msg'+(kind?' '+kind:'');m.setAttribute('role',kind==='err'?'alert':'status');}
function setBusy(btn,on,label,orig){btn.classList.toggle('loading',on);btn.disabled=on;const l=btn.querySelector('.lbl');if(l)l.textContent=on&&label?label:orig;}
function view(name,keepMsg){
  for(const id of ['vSign','vReg','vCode','vDone']){const el=$(id);if(el)el.hidden=(id!==name);}
  current=name;if(!keepMsg)say('');
  const h=$(name).querySelector('h1');if(h)h.focus({preventScroll:true});
  const first=$(name).querySelector('input:not([type=hidden]),textarea');if(first)first.focus({preventScroll:true});
}
async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:H,credentials:'same-origin',body:body===undefined?undefined:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));return [r.status,j];
}
function mmss(s){s=Math.max(0,Math.round(s));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}
function success(j){say('Signed in as '+j.user+'. Opening the console\u2026','ok');setBusy(go,true,'Opening\u2026',LBL0);location.replace(next);}
function lock(sec){
  locked=true;clearInterval(lockTimer);const end=Date.now()+sec*1000;setBusy(go,false,'',LBL0);go.disabled=true;
  const tick=()=>{const left=(end-Date.now())/1000;
    if(left<=0){clearInterval(lockTimer);locked=false;go.disabled=false;say('You can try again now.','info');return;}
    say('Too many attempts. Try again in '+mmss(left)+'.','warn');};
  tick();lockTimer=setInterval(tick,1000);
}
async function poll(){
  try{
    const r=await fetch('/auth/poll',{headers:H,credentials:'same-origin'});const j=await r.json().catch(()=>({}));
    if(j.state==='running'){setTimeout(poll,1000);return;}
    if(j.state==='done'){success(j);return;}
    say(j.reason||'Sign-in failed.','err');
  }catch(e){say(NETERR,'err');}
  setBusy(go,false,'',LBL0);busy=false;
}
function caps(e){const t=e.target.closest&&e.target.closest('.field');if(!t)return;const c=t.parentElement.querySelector('.caps');
  if(c&&e.getModifierState)c.hidden=!e.getModifierState('CapsLock');}
document.addEventListener('keydown',caps);document.addEventListener('keyup',caps);
document.addEventListener('click',e=>{
  const eye=e.target.closest('.eye');
  if(eye){const on=eye.getAttribute('aria-pressed')!=='true';eye.setAttribute('aria-pressed',on?'true':'false');
    eye.setAttribute('aria-label',on?'Hide password':'Show password');
    for(const id of eye.dataset.for.split(' ')){const i=$(id);if(i)i.type=on?'text':'password';}return;}
  if(e.target.closest('#toReg'))view('vReg');
  if(e.target.closest('.toSign'))view('vSign');
});
if(!TOKEN){say(NOTOKEN,'err');go.disabled=true;}
$('fSign').addEventListener('submit',async e=>{
  e.preventDefault();if(busy||locked)return;
  if(!TOKEN){say(NOTOKEN,'err');return;}
  if(MODE==='accounts'){
    const u=$('u').value.trim(),p=$('p').value;
    $('u').classList.remove('bad');$('p').classList.remove('bad');
    if(!u||!p){say('Enter your username and password.','err');if(!u)$('u').classList.add('bad');if(!p)$('p').classList.add('bad');(u?$('p'):$('u')).focus();return;}
    busy=true;setBusy(go,true,'Signing in\u2026',LBL0);say('');
    try{
      const [st,j]=await post('/auth/login',{username:u,password:p});$('p').value='';
      if(st===200){success(j);return;}
      if(st===429){busy=false;lock(j.retry_after_s||60);return;}
      say(j.message||('Sign-in failed ('+st+').'),j.error==='pending'?'info':'err');
      if(j.error==='invalid_credentials'){$('p').classList.add('bad');$('p').focus();}
    }catch(err){say(NETERR,'err');}
    busy=false;setBusy(go,false,'',LBL0);return;
  }
  busy=true;setBusy(go,true,MODE==='windows'?'Waiting for Windows\u2026':'Signing in\u2026',LBL0);
  say(MODE==='windows'?'Complete the Windows Security prompt. If it is not in front, check the taskbar.'
      :MODE==='entra'?'Complete the sign-in in the browser window that opened.':'Signing in\u2026','busy');
  try{
    const r=await fetch('/auth/login',{method:'POST',headers:H,credentials:'same-origin'});
    if(r.status===202){poll();return;}
    const j=await r.json().catch(()=>({}));
    say(j.error==='sign_in_in_progress'?'A sign-in is already open. Finish it first.':(j.detail||'Could not start sign-in ('+r.status+').'),'err');
  }catch(err){say(NETERR,'err');}
  busy=false;setBusy(go,false,'',LBL0);
});
if($('fReg')){
  const rn=$('rn'),re_=$('re'),ru=$('ru'),rp=$('rp'),rp2=$('rp2'),rr=$('rr'),goReg=$('goReg'),goCode=$('goCode'),rc=$('rc'),rs=$('resend');
  const USER=/^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/;
  const R0=goReg.querySelector('.lbl').textContent, C0=goCode.querySelector('.lbl').textContent;
  let vid='', expiresAt=0, resendAt=0, timer=null;
  function tick(){
    const now=Date.now(), left=(expiresAt-now)/1000, wait=(resendAt-now)/1000;
    $('expiry').textContent=left>0?'The code expires in '+mmss(left)+'.':'The code has expired. Send a new code.';
    rs.disabled=wait>0;rs.textContent=wait>0?'Send a new code ('+Math.ceil(wait)+'s)':'Send a new code';
    goCode.disabled=left<=0||goCode.classList.contains('loading');
  }
  function arm(j){vid=j.id||vid;expiresAt=Date.now()+j.expires_in_s*1000;resendAt=Date.now()+j.resend_in_s*1000;
    $('sentTo').textContent=j.email||'';clearInterval(timer);tick();timer=setInterval(tick,1000);}
  $('fReg').addEventListener('submit',async e=>{
    e.preventDefault();if(!TOKEN){say(NOTOKEN,'err');return;}
    [rn,re_,ru,rp,rp2].forEach(i=>i.classList.remove('bad'));
    const bad=(el,text)=>{el.classList.add('bad');el.focus();say(text,'err');};
    if(!rn.value.trim())return bad(rn,'Enter your full name.');
    if(!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(re_.value.trim()))return bad(re_,'Enter your work email address.');
    if(!USER.test(ru.value.trim()))return bad(ru,'Username: 3\u201332 letters, digits, dot, dash or underscore, starting with a letter or digit.');
    if(rp.value.length<MINPW)return bad(rp,'Use at least '+MINPW+' characters for the password.');
    if(rp.value!==rp2.value)return bad(rp2,'The passwords do not match.');
    setBusy(goReg,true,'Sending code\u2026',R0);say('');
    try{
      const [st,j]=await post('/auth/register/start',{name:rn.value,email:re_.value.trim(),username:ru.value.trim(),password:rp.value,reason:rr.value});
      if(st===202){rp.value='';rp2.value='';rc.value='';arm(j);setBusy(goReg,false,'',R0);view('vCode');return;}
      say(j.message||('Could not send the request ('+st+').'),'err');
      if(j.error==='domain_not_allowed'||j.error==='bad_email'||j.error==='email_taken')re_.classList.add('bad');
      if(j.error==='username_taken'||j.error==='bad_username')ru.classList.add('bad');
      if(j.error==='weak_password'){rp.classList.add('bad');}
    }catch(err){say(NETERR,'err');}
    setBusy(goReg,false,'',R0);
  });
  rc.addEventListener('input',()=>{const v=rc.value.replace(/\D/g,'').slice(0,6);if(v!==rc.value)rc.value=v;rc.classList.remove('bad');});
  $('fCode').addEventListener('submit',async e=>{
    e.preventDefault();
    if(!/^\d{6}$/.test(rc.value)){rc.classList.add('bad');rc.focus();say('Enter the 6-digit code from the email.','err');return;}
    setBusy(goCode,true,'Verifying\u2026',C0);say('');
    try{
      const [st,j]=await post('/auth/register/verify',{id:vid,code:rc.value});
      if(st===201){clearInterval(timer);$('doneText').textContent=j.message||'';$('fReg').reset();view('vDone');setBusy(goCode,false,'',C0);return;}
      say(j.message||('Could not verify the code ('+st+').'),'err');rc.classList.add('bad');rc.select();
      if(j.error==='expired'){expiresAt=Date.now();}
      if(j.error==='not_found'||j.error==='too_many_attempts'){clearInterval(timer);vid='';$('restart').hidden=false;goCode.disabled=true;rs.disabled=true;}
    }catch(err){say(NETERR,'err');}
    setBusy(goCode,false,'',C0);tick();
  });
  rs.addEventListener('click',async()=>{
    rs.disabled=true;say('');
    try{
      const [st,j]=await post('/auth/register/resend',{id:vid});
      if(st===200){arm(j);rc.value='';rc.focus();say('A new code has been sent. Earlier codes no longer work.','info');return;}
      if(j.resend_in_s!==undefined)resendAt=Date.now()+j.resend_in_s*1000;
      say(j.message||('Could not send a new code ('+st+').'),'err');
      if(j.error==='not_found'||j.error==='too_many_codes'){$('restart').hidden=false;}
    }catch(err){say(NETERR,'err');}
    tick();
  });
  document.addEventListener('click',e=>{if(e.target.closest('.toDetails')){clearInterval(timer);vid='';$('restart').hidden=true;view('vReg');}});
}
if(MODE==='accounts'&&$('u'))$('u').focus();else go.focus();
})();
</script></body></html>"""

FIELDS_ACCOUNTS = """
   <label for="u">Username</label>
   <div class="field"><input id="u" name="username" autocomplete="username" maxlength="32" spellcheck="false" autocapitalize="none"></div>
   <label for="p">Password</label>
   <div class="field"><input class="pw" id="p" name="password" type="password" autocomplete="current-password" maxlength="256">
    <button class="eye" type="button" data-for="p" aria-pressed="false" aria-label="Show password">__EYE__</button></div>
   <p class="caps" hidden role="status">Caps Lock is on</p>"""

REG_LINK = '<div class="links"><button class="lnk" id="toReg" type="button">Request access</button></div>'

REG_VIEWS = """
 <section class="view" id="vReg" hidden aria-labelledby="hReg">
  <p class="step">Step 1 of 2</p>
  <h1 id="hReg" tabindex="-1">Request access</h1>
  <p class="sub">Use your work email. We'll send a code to confirm it before your request goes to a manager.</p>
  <div class="msg" role="status" aria-live="polite"></div>
  <form id="fReg" novalidate autocomplete="on">
   <label for="rn">Full name</label><div class="field"><input id="rn" name="name" autocomplete="name" maxlength="80"></div>
   <label for="re">Work email</label><div class="field"><input id="re" name="email" type="email" autocomplete="email" maxlength="200" spellcheck="false"></div>
   <label for="ru">Username</label><div class="field"><input id="ru" name="username" autocomplete="username" maxlength="32" spellcheck="false" autocapitalize="none"></div>
   <label for="rp">Password</label>
   <div class="field"><input class="pw" id="rp" name="password" type="password" autocomplete="new-password" maxlength="128">
    <button class="eye" type="button" data-for="rp rp2" aria-pressed="false" aria-label="Show password">__EYE__</button></div>
   <p class="hint">At least __MINPW__ characters. Don't include your username.</p>
   <p class="caps" hidden role="status">Caps Lock is on</p>
   <label for="rp2">Confirm password</label><div class="field"><input class="pw" id="rp2" type="password" autocomplete="new-password" maxlength="128"></div>
   <label for="rr">Reason <span class="opt">(optional)</span></label><div class="field"><textarea id="rr" name="reason" rows="2" maxlength="500"></textarea></div>
   <div class="actions"><button class="go" id="goReg" type="submit"><span class="lbl">Continue</span><span class="spin" aria-hidden="true"></span></button></div>
  </form>
  <div class="links"><button class="lnk toSign" type="button">Back to sign in</button></div>
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
   <div class="actions"><button class="go" id="goCode" type="submit"><span class="lbl">Verify and submit</span><span class="spin" aria-hidden="true"></span></button></div>
  </form>
  <div class="links"><button class="lnk" id="resend" type="button" disabled>Send a new code</button><button class="lnk toDetails" type="button">Change details</button></div>
  <div class="links" id="restart" hidden><button class="lnk toDetails" type="button">Start again</button></div>
 </section>
 <section class="view" id="vDone" hidden aria-labelledby="hDone">
  <div class="done"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg></div>
  <h1 id="hDone" tabindex="-1">Request sent</h1>
  <p class="sub" id="doneText"></p>
  <div class="msg" role="status" aria-live="polite"></div>
  <div class="actions"><button class="go alt toSign" type="button"><span class="lbl">Back to sign in</span></button></div>
 </section>"""


def _csp(nonce: str) -> str:
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def render(mode: str, nxt: str = "/", nonce: str | None = None, registration: bool = False,
           min_password: int = 12, code_minutes: int = 15, idle_minutes: int | None = None) -> tuple[str, str]:
    """Return (html, csp). nxt is where to go after sign-in (only same-site paths are honoured).
    idle_minutes is accepted for compatibility and no longer shown."""
    if mode not in MODES:
        raise ValueError(f"unknown sign-in mode: {mode}")
    nxt = nxt or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        nxt = "/"                                           # no open redirect
    nonce = nonce or secrets.token_urlsafe(16)
    title, note, button, icon = COPY[mode]
    accounts = mode == "accounts"
    reg = accounts and bool(registration)
    fields = FIELDS_ACCOUNTS.replace("__EYE__", ICON_EYE) if accounts else ""
    reg_views = REG_VIEWS.replace("__EYE__", ICON_EYE) if reg else ""
    page = PAGE
    for key, val in (("__MARK__", MARK), ("__FIELDS__", fields), ("__REGLINK__", REG_LINK if reg else ""),
                     ("__REGVIEWS__", reg_views), ("__TITLE__", _html.escape(title)), ("__NOTE__", _html.escape(note)),
                     ("__BUTTON__", _html.escape(button)), ("__ICON__", icon)):
        page = page.replace(key, val)
    for key, val in (("__NONCE__", nonce), ("__NEXT__", json.dumps(nxt).replace("<", "\\u003c")),
                     ("__MODE__", json.dumps(mode)), ("__REG__", "true" if reg else "false"),
                     ("__MINPW__", str(int(min_password))), ("__MINUTES__", str(int(code_minutes)))):
        page = page.replace(key, val)
    return page, _csp(nonce)
