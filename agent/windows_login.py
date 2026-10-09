"""windows_login.py - ask Windows to prove who is at the keyboard (v13).

The console's "Sign in with Windows" opens the same Windows Security window you see when you unlock your
laptop, and trusts the answer only if Windows says the person passed:

  hello     Windows Hello: your PIN, fingerprint or face (the UserConsentVerifier API, called through the
            Windows PowerShell that ships with Windows; nothing to install). It re-verifies the account that
            is signed in to this Windows session.
  password  Windows Security credential window (CredUIPromptForWindowsCredentials) + LogonUser: asks for a
            Windows username and password and has Windows check them. Works for local and domain accounts.
            Windows may refuse an Entra-only password here; Hello is the normal path on a work laptop.
  auto      hello first; if Hello is not set up or not available on this PC, the password window.

What this proves (and does not): that the person at the keyboard knows this Windows account's PIN, fingerprint,
face or password. It does not protect against malware already running inside your Windows session.
Standard library only. Windows only.
"""
from __future__ import annotations

import base64
import getpass
import os
import subprocess

NO_WINDOW = 0x08000000
ERROR_CANCELLED = 1223
ERROR_INSUFFICIENT_BUFFER = 122
LOGON32_LOGON_NETWORK, LOGON32_PROVIDER_DEFAULT = 3, 0

HELLO_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    [void]$t.Wait(-1)
    return $t.Result
  }
  [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType = WindowsRuntime] | Out-Null
  [Windows.Security.Credentials.UI.UserConsentVerifierAvailability, Windows.Security.Credentials.UI, ContentType = WindowsRuntime] | Out-Null
  [Windows.Security.Credentials.UI.UserConsentVerificationResult, Windows.Security.Credentials.UI, ContentType = WindowsRuntime] | Out-Null
  $a = Await ([Windows.Security.Credentials.UI.UserConsentVerifier]::CheckAvailabilityAsync()) ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
  if ("$a" -ne 'Available') { Write-Output "AVAILABILITY:$a"; exit 0 }
  $r = Await ([Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync($env:AIDC_HELLO_MESSAGE)) ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
  Write-Output "RESULT:$r"
} catch {
  Write-Output "ERROR:$($_.Exception.Message)"
}
"""


def current_identity() -> str:
    dom = os.environ.get("USERDOMAIN", "")
    return f"{dom}\\{getpass.getuser()}" if dom else getpass.getuser()


# --------------------------------------------------------------------------- Windows Hello
def parse_hello(output: str):
    """('verified'|'canceled'|'unavailable'|'failed'|'error', detail) from the helper's last marker line."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith("RESULT:"):
            r = line[7:].strip()
            if r == "Verified":
                return "verified", r
            if r == "Canceled":
                return "canceled", r
            if r in ("DeviceNotPresent", "NotConfiguredForUser", "DisabledByPolicy"):
                return "unavailable", r
            return "failed", r
        if line.startswith("AVAILABILITY:"):
            return "unavailable", line[13:].strip()
        if line.startswith("ERROR:"):
            return "error", line[6:].strip()[:200]
    return "error", "no answer from Windows"


def hello_verify(message: str, timeout: int = 200, runner=subprocess.run):
    if os.name != "nt":
        return "unavailable", "not Windows"
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
           base64.b64encode(HELLO_SCRIPT.encode("utf-16-le")).decode("ascii")]
    env = dict(os.environ, AIDC_HELLO_MESSAGE=message)
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=timeout, env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "failed", "timed out waiting for the Windows prompt"
    except OSError as exc:
        return "unavailable", f"PowerShell could not start: {exc}"
    return parse_hello(r.stdout)


# --------------------------------------------------------------------------- password window
def _credui_prompt(message: str, caption: str = "AI Delivery Console"):
    """Show the Windows credential window. Returns (username, domain, password). Raises PermissionError if cancelled."""
    import ctypes
    from ctypes import wintypes

    class CREDUI_INFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hwndParent", wintypes.HWND), ("pszMessageText", wintypes.LPCWSTR),
                    ("pszCaptionText", wintypes.LPCWSTR), ("hbmBanner", wintypes.HBITMAP)]

    credui, ole32 = ctypes.WinDLL("credui", use_last_error=True), ctypes.WinDLL("ole32")
    credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(CREDUI_INFOW), wintypes.DWORD, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, wintypes.ULONG,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.BOOL), wintypes.DWORD]
    credui.CredUIPromptForWindowsCredentialsW.restype = wintypes.DWORD
    credui.CredUnPackAuthenticationBufferW.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    credui.CredUnPackAuthenticationBufferW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    info = CREDUI_INFOW(ctypes.sizeof(CREDUI_INFOW), None, message, caption, None)
    package, out_buf, out_size, save = wintypes.ULONG(0), ctypes.c_void_p(), wintypes.ULONG(0), wintypes.BOOL(False)
    rc = credui.CredUIPromptForWindowsCredentialsW(ctypes.byref(info), 0, ctypes.byref(package), None, 0,
                                                   ctypes.byref(out_buf), ctypes.byref(out_size), ctypes.byref(save), 0)
    if rc == ERROR_CANCELLED:
        raise PermissionError("Windows sign-in was cancelled.")
    if rc != 0:
        raise PermissionError(f"The Windows credential window failed (error {rc}).")
    try:
        for flags in (0, 1):                                    # 1 = CRED_PACK_PROTECTED_CREDENTIALS
            cu, cd, cp = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
            credui.CredUnPackAuthenticationBufferW(flags, out_buf, out_size.value, None, ctypes.byref(cu), None,
                                                   ctypes.byref(cd), None, ctypes.byref(cp))
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER and not (cu.value or cp.value):
                continue
            user, dom, pw = (ctypes.create_unicode_buffer(max(1, x.value)) for x in (cu, cd, cp))
            if credui.CredUnPackAuthenticationBufferW(flags, out_buf, out_size.value, user, ctypes.byref(cu), dom,
                                                      ctypes.byref(cd), pw, ctypes.byref(cp)):
                try:
                    return user.value, dom.value, pw.value
                finally:
                    ctypes.memset(pw, 0, ctypes.sizeof(pw))
        raise PermissionError("Windows returned credentials in a form that cannot be checked here "
                              "(this happens with a PIN). Use Windows Hello or a password.")
    finally:
        ole32.CoTaskMemFree(out_buf)


def _logon_user(username: str, domain: str, password: str) -> bool:
    import ctypes
    from ctypes import wintypes
    adv, k32 = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32")
    adv.LogonUserW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(wintypes.HANDLE)]
    adv.LogonUserW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    ok = adv.LogonUserW(username, domain or None, password, LOGON32_LOGON_NETWORK, LOGON32_PROVIDER_DEFAULT,
                        ctypes.byref(token))
    if ok:
        k32.CloseHandle(token)
    return bool(ok)


def password_login(message: str, prompt=_credui_prompt, validate=_logon_user) -> str:
    """The Windows credential window plus a Windows check. Returns DOMAIN\\user. Raises PermissionError."""
    if os.name != "nt" and prompt is _credui_prompt:
        raise PermissionError("The Windows credential window only exists on Windows.")
    user, domain, password = prompt(message)
    try:
        if not validate(user, domain, password):
            raise PermissionError("Windows did not accept that username and password.")
    finally:
        password = ""
    if "\\" in user or "@" in user:
        return user
    return f"{domain}\\{user}" if domain else user


# --------------------------------------------------------------------------- the verifier used by the gate
class WindowsVerifier:
    """Callable returning {"_windows": identity, "name": ..., "method": ...} or raising PermissionError."""

    def __init__(self, method="auto", message="Sign in to the AI Delivery Console", identity=current_identity,
                 hello=hello_verify, password=password_login):
        if method not in ("auto", "hello", "password"):
            raise ValueError("method must be auto, hello or password")
        self.method, self.message, self.identity, self.hello, self.password = method, message, identity, hello, password

    def __call__(self):
        order = {"auto": ("hello", "password"), "hello": ("hello",), "password": ("password",)}[self.method]
        notes = []
        for m in order:
            if m == "hello":
                status, detail = self.hello(self.message)
                if status == "verified":
                    who = self.identity()
                    return {"_windows": who, "name": who, "method": "hello"}
                if status == "canceled":
                    raise PermissionError("Windows sign-in was cancelled.")
                if status == "failed":
                    raise PermissionError(f"Windows Hello did not verify you ({detail}).")
                notes.append(f"Windows Hello unavailable ({detail})")
                if self.method == "hello":
                    raise PermissionError(notes[-1] + ". Set it up in Windows Settings > Accounts > Sign-in options, "
                                          "or set CONSOLE_WINDOWS_VERIFY=password.")
            else:
                who = self.password(self.message)
                return {"_windows": who, "name": who, "method": "password"}
        raise PermissionError("No Windows sign-in method worked: " + "; ".join(notes))
