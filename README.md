# Windows VPN Credential Updater: change the saved username & password on many VPN profiles at once

> A tiny Windows 11 / Windows 10 tool that updates the **saved username and password of all your built-in IKEv2 VPN profiles in one click**. No more opening Settings > Network & internet > VPN and editing profiles one by one every time your VPN provider rotates credentials.

Made by **Micro**. Join my Telegram channel for more tools: **[@MicroLab_SecZero](https://t.me/MicroLab_SecZero)**

---

## So what is this?

If you use the built-in Windows VPN client (not a third-party app) and your provider gave you a bunch of IKEv2 profiles, you know the pain: the provider changes your credentials, and now you have to edit **every single profile** by hand. Click, edit, retype username, retype password, save, repeat... 20 times.

This tool fixes that:

1. It lists all your VPN profiles (per-user _and_ all-user).
2. You type the new username and password **once**.
3. You tick the profiles you want.
4. You hit **Apply**. Done.

Next time you connect, Windows uses the new credentials and doesn't nag you for them.

## Features

- Lists every built-in Windows VPN profile, both **current-user** and **all-users**
- Searchable, scrollable checklist with **Select all / Select none / Refresh**
- One username + one password field (with a show/hide toggle)
- Per-profile result list: success, or the exact error Windows gave
- Progress bar, and the UI never freezes because the work runs in a background thread
- Clean modern look with CustomTkinter, follows your Windows light/dark theme, DPI-aware
- Warns you if a selected profile is currently connected (the new credentials apply on the _next_ connection)
- Detects when you need admin rights and gives you a **Restart as administrator** button

## What it does NOT do

It only touches the saved credentials. It does **not** change the server, VPN type, auth method, or any other profile setting. It also doesn't create, connect, disconnect or export profiles. Small tool, one job.

## Requirements

- Windows 10 / 11
- Python 3.11 or newer
- PowerShell (it ships with Windows, the tool uses it to list your profiles)

## Quick start

```
pip install -r requirements.txt
python vpn_cred_updater.py
```

Then:

1. Type the new **username** and **password**.
2. Tick the profiles you want to update (or hit **Select all**).
3. Click **Apply** and check the result list.

### About admin rights

- **Per-user profiles** (the ones that show up only for your Windows account) work with a normal, non-elevated app.
- **All-user profiles** need administrator rights. If you tick one without being elevated, the app shows a warning and a **Restart as administrator** button. Click it, approve the UAC prompt, and you're good.

## Is it safe? What happens to my password?

- The password is **never written to disk, logs, or the console**.
- It's passed straight to the official Windows API (`RasSetCredentialsW` in `rasapi32.dll`) to save it the same way Windows does when you type it in Settings.
- After you click Apply, the password field is cleared and the internal buffer is zeroed.
- The script doesn't touch `rasphone.pbk` by hand and doesn't talk to the network. Read the code, it's a single file.

One honest caveat: Python strings are immutable, so a copy of the password may sit in memory until Python's garbage collector cleans it up. That's a Python thing, not something this tool can fully control.

## How it works (for the curious)

- Profiles are listed with PowerShell's `Get-VpnConnection` and `Get-VpnConnection -AllUserConnection`, returned as JSON.
- Credentials are set with the Win32 function `RasSetCredentialsW` called through `ctypes`, with `RASCM_UserName | RASCM_Password` and `fClearCredentials = FALSE`.
- Errors are translated into readable text with `RasGetErrorStringW`.
- The only dependency is `customtkinter`. Everything else is the Python standard library.

## FAQ

**How do I change the saved VPN password on multiple Windows VPN profiles at once?**
Use this tool: enter the new credentials once, tick your profiles, click Apply.

**Does it work with IKEv2, L2TP, SSTP or PPTP profiles?**
It's built and aimed at built-in IKEv2 profiles using "Username and password" sign-in. Since it uses the generic Windows RAS credential API it may work with other built-in VPN types that store a username and password, but I haven't tested those.

**Will it work with third-party VPN apps like OpenVPN or WireGuard?**
No. It only handles profiles you see in Windows' own VPN settings.

**Windows still asks me for credentials after updating. Why?**
Make sure the profile has "Remember my sign-in info" turned on and uses "Username and password" as the sign-in type. This tool only updates the saved credentials, it doesn't flip those settings.

**I updated a profile that was connected. Is it using the new password now?**
No. The new credentials are used the next time you connect.

**Why do all-user profiles need admin?**
They live in a system-wide phonebook that normal accounts can't write to.

## Known limitations

I wrote this carefully but I can't test every Windows setup. Things I'm not 100% sure about:

- Whether Windows accepts the change cleanly while a profile is **actively connected** (the app warns you first, and any error shows up in the result list).
- Whether credentials saved to an **all-user** profile are shared by every Windows user or stored per user. Connect once to verify.
- Profiles stored in unusual phonebook locations (like `_hiddenPbk`) aren't handled.
- If you elevate using a _different_ admin account, "current user" profiles refer to that account, not yours.
- "Select all / none" only affects the profiles visible under your current filter.

Test on one profile first if you're cautious. If you find a bug, open an issue.

## Contributing

Issues and pull requests are welcome. Keep it small and focused: this tool does one thing on purpose.

## More from me

I share more small security and utility tools on Telegram: **[@MicroLab_SecZero](https://t.me/MicroLab_SecZero)**. Come say hi.

## License

MIT. [LICENSE](LICENSE).

---

<sub>Keywords: windows vpn credential updater, change vpn password multiple profiles, bulk update windows vpn username password, windows 11 vpn profiles, ikev2 vpn saved credentials, RasSetCredentials python, rasapi32 ctypes, Get-VpnConnection, customtkinter windows tool.</sub>
