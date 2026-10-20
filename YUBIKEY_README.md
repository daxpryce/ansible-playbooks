# YubiKey PIN + Touch Authentication

Run the commands below from the repository root as your normal workstation
account unless a step explicitly says otherwise.

This is **optional** and does not run in the full workstation build unless
`yubikey_pam_enabled=true`. The standalone playbook defaults to installing tools
only; it never enrolls a key or changes PAM until explicitly activated.
Use a FIDO2-capable YubiKey (such as a YubiKey 5 or Security Key), not an older
U2F-only key.

Authentication accepts **either the key's FIDO2 PIN plus touch, or your existing
Linux password**. This does not require a Yubico cloud service. The PIN stays
on the key and is entered interactively, never in Ansible variables or files.
The FIDO2 PIN is shared with other FIDO2 uses of that same key.
For `sudo` inside a positively identified SSH session, the key is skipped and
the existing Linux-password stack is used directly, even if the key stays
plugged into the workstation. Local sudo, login, and unlock keep PIN + touch
with password fallback. SSH login authentication itself is unchanged.

## 1. Install the tools

Normally run as your workstation account, with per-task privilege escalation:

```bash
ansible-playbook playbooks/yubikey_pam.yml -K
```

## 2. Check or set the FIDO2 PIN

Insert only the key you intend to enroll:

```bash
ykman info
ykman fido info
```

If it does not already have a FIDO2 PIN, set one interactively:

```bash
ykman fido access change-pin
```

If a PIN already exists, use that PIN; changing it is not necessary. Do not
reset FIDO to recover a forgotten PIN: a reset deletes the key's existing
FIDO credentials, including ones used for websites. Repeated wrong PIN
attempts can block the key.

## 3. Enroll the key

Run as the account that will log in. This prompts for the FIDO2 PIN and touch.
The fixed origin below must match `yubikey_pam_origin`; using a fixed value
also avoids breaking authentication if the workstation's hostname changes.

```bash
install -d -m 700 ~/.config/Yubico
# Capture first so a failed enrollment cannot truncate an existing mapping.
registration="$(pamu2fcfg -N -o pam://yubikey-pam -i pam://yubikey-pam)" &&
  (umask 077; printf '%s\n' "$registration" > ~/.config/Yubico/u2f_keys)
unset registration
```

This replaces the local registration file on success. If you already use
`~/.config/Yubico/u2f_keys` for another setup, back it up first or use a separate
file and pass its absolute path as `yubikey_pam_registration_file`.

**Recommended: enroll a backup key.** Unplug the first key, insert the backup,
check/set its FIDO2 PIN as above, then append its credential to the same line:

```bash
registration="$(pamu2fcfg -N -n -o pam://yubikey-pam -i pam://yubikey-pam)" &&
  existing="$(cat ~/.config/Yubico/u2f_keys)" &&
  (umask 077; printf '%s%s\n' "$existing" "$registration" > ~/.config/Yubico/u2f_keys)
unset registration existing
```

Re-run activation whenever you add or replace keys; PAM uses a root-owned
copy of the mapping, not this user-writable enrollment file.

## 4. Activate PAM

Keep a separate root shell open (`sudo -i`) until you have tested both key and
password login. For logout/login tests, use a text console (Ctrl+Alt+F3,
then log in and run `sudo -i`): a terminal in your graphical session will
close when you log out. Use another terminal for Ansible and the tests below.
If your keyboard cannot switch consoles, have the GRUB recovery instructions
below available on another device before logging out.

**Unplug all YubiKeys when using Ansible's `-K` method below.**
Enrollment is already saved in the registration file, so activation does not
need the key connected. Ansible's `-K` password is your **Linux password**, not
the FIDO2 PIN. With a key connected, PAM may request its PIN first; Ansible
cannot handle this extra prompt and may send your Linux password as a wrong
PIN before failing with "Duplicate become password prompt". Do not repeatedly
retry with the key connected, because wrong PIN attempts can block it.
Keep keys unplugged for other sudo-based Ansible runs using `-K` too.

```bash
ansible-playbook playbooks/yubikey_pam.yml -K \
  -e yubikey_pam_state=configured --check --diff

ansible-playbook playbooks/yubikey_pam.yml -K \
  -e yubikey_pam_state=configured
```

**Alternative with the key connected:** from your normal user shell, launch
this local playbook through a single interactive sudo call:

```bash
sudo -- "$(command -v ansible-playbook)" playbooks/yubikey_pam.yml \
  -e ansible_become=false \
  -e yubikey_pam_state=configured \
  -e yubikey_pam_user="$(id -un)" \
  -e yubikey_pam_registration_file="$HOME/.config/Yubico/u2f_keys"
```

Sudo handles PIN and touch interactively before Ansible starts. The entire
playbook then runs as root, with further privilege escalation disabled.
The explicit user and registration path are essential: the calling shell
expands them before sudo, preventing root's gathered facts from selecting the
wrong account or home directory. Do not run this command from a root shell.
Add `--check --diff` for a preview, or change the state to `absent` to disable.
This is a local-playbook recovery/maintenance path, not a recommendation to
run the full workstation build as root.

Do not rely on `sudo -v` followed by Ansible without `-K`: a successful
`sudo -n true` in your terminal does not prove Ansible's sudo subprocess will
share that authentication timestamp. That approach failed on this workstation.

The role requires an enrolled FIDO2 credential before changing PAM. It manages
only `sudo`, `lightdm`, `cinnamon-screensaver`, and `login` service files, inserts
its rules immediately before their existing `system-auth` authentication line,
and leaves that password stack intact. `sudo -i` inherits the `sudo` stack.
Other accounts without a mapping continue to use passwords.
To restrict the services, pass a YAML variables file containing, for example:

```yaml
yubikey_pam_services:
  - sudo
```

Use `-e @/absolute/path/to/variables.yml` with each subsequent run. The defaults
target Fedora Cinnamon/LightDM; on another desktop, select only the supported
services you actually use. Missing, symlinked, or unexpected service files cause
activation to stop before editing PAM. Removing a service from the list does
not remove previously installed rules: run `state=absent` with the old list
first, then activate the new list.

Credentials are stored in `/etc/security/yubikey-pam/u2f_keys`, owned by root
with mode `0644` in a `0755` directory and persistent SELinux labeling.
Cinnamon's PAM helper runs as the logged-in user and must be able to read it.
The mapping contains public keys and credential handles, not private keys or
PINs; only root can modify it. Existing mappings for other users are preserved.
No authselect profile is switched, and generated
`system-auth`/`password-auth` files are not edited. Do not enable authselect's
`with-pam-u2f` features for this setup; that would add a separate shared rule.
The role backs up changed PAM files and restores them if a PAM write fails.
Activation updates sudo last; removal updates it first, to avoid changing
Ansible's own authentication between service updates. Rollback also requires
working privilege escalation, so leave keys unplugged for the entire run.
That unplugging requirement applies to the `-K` method; the single interactive
sudo invocation above can keep the key connected.

## 5. Test before closing the recovery shell

With a registered key inserted:

```bash
sudo -k
sudo -v
```

Enter the **FIDO2 PIN**, then touch the flashing key. Remove the key and repeat:
your normal **Linux password** must still work. A failed or canceled FIDO
attempt also falls through to the password stack; a connected key may need to
time out before that prompt appears.

Test screen lock/unlock and a separate text console (Ctrl+Alt+F3; the graphical
session is typically on Ctrl+Alt+F1 or F2) with both methods before logging out.
Finally test the LightDM login screen. Do not reboot as the first test.

This does not change sudo's authentication cache, SSH login, automatic login,
LUKS disk unlock, or desktop keyring encryption. Without your Linux password,
the login keyring may remain locked and ask for its password separately.
LightDM greeter wording can vary, so distinguish the FIDO2 PIN prompt from
the fallback account-password prompt.

**Verified on the Fedora Cinnamon workstation:** sudo accepts PIN + touch
with the key connected and the Linux password with it disconnected; Cinnamon
screen unlock also accepts PIN + touch after correcting the mapping permissions.
No logout, session restart, or reboot was needed for that fix. LightDM and
console login are configured too, but must still be tested separately.

If Cinnamon offers only password authentication, check the mapping permissions
before restarting the session. Its PAM helper runs unprivileged and cannot
read a root-only `0700` directory or `0600` file. Earlier revisions of this
role used those modes; rerun activation using either supported method above
to apply `0755`/`0644`. Keep root ownership: readability is required, but
user-writability is not. There is also an
[upstream compatibility report](https://github.com/linuxmint/cinnamon-screensaver/issues/516);
it does not mean PIN + touch is universally unsupported on Cinnamon.

### Sudo over SSH

Rerun activation from step 4 to update an existing installation; no reenrollment
is needed. The connected-key, single interactive sudo command works for this
update too. Afterward, open a fresh SSH connection to the workstation and run:

```bash
loginctl show-session self -p Remote -p Service
sudo -k
sudo -v
```

The session should report `Remote=yes` and `Service=sshd`. Sudo should ask for
your **Linux password**, without requesting the key's PIN or touch. Repeat the
sudo commands in a local desktop terminal: that should still request PIN +
touch. Do not enter deliberately wrong PINs to force password fallback.

The root-owned `/usr/local/bin/yubikey-pam-is-ssh` helper queries logind for its
own process's session, with a clean subprocess environment and a five-second
timeout. It does not trust `SSH_CONNECTION`, `SSH_TTY`, or `XDG_SESSION_ID`.
Only `Remote=yes` together with `Service=sshd` skips the single following
`pam_u2f` rule; the original password stack is never skipped by this guard.
Missing sessions, logind failures, or unexpected metadata retain the normal
YubiKey-first behavior and log a warning:

```bash
journalctl -t yubikey-pam-is-ssh --since "10 minutes ago"
```

This follows the process's logind session, not whichever client is currently
attached to it. A tmux or screen server started locally can still look local
when attached over SSH; a server started over SSH can retain its remote
classification when attached locally. Use a fresh SSH shell outside an
existing multiplexer when checking this behavior.

## Disable the integration

```bash
ansible-playbook playbooks/yubikey_pam.yml -K -e yubikey_pam_state=absent
```

This removes this role's marked PAM blocks and, when `sudo` is selected, its
SSH session helper. Packages and credentials remain in place.
Merely running `setup` again does not disable existing rules. Use the same
service list you used to activate. From the recovery root shell, you can also
remove the entire marked block, including both markers, between
`# BEGIN ANSIBLE MANAGED YUBIKEY PAM` and
`# END ANSIBLE MANAGED YUBIKEY PAM` from each affected `/etc/pam.d/` file.
Do not remove the original `system-auth` lines.

If an earlier run changed sudo and then failed with a duplicate password
prompt, unplug the key and rerun activation with `-K` and your Linux password.
The existing sudo block is reused, and the remaining services are configured.
Alternatively, run the disable command with the key unplugged to remove the
partially applied integration.

## Emergency recovery from GRUB

If you cannot authenticate, select the Fedora boot entry in GRUB, press **e**,
append `rd.break` to the kernel's `linux` line, and boot with **Ctrl+X**.
You still need your disk-encryption passphrase if applicable.

With the YubiKey unplugged, run:

```bash
mount -o remount,rw /sysroot
chroot /sysroot
sed -i '/^# BEGIN ANSIBLE MANAGED YUBIKEY PAM$/,/^# END ANSIBLE MANAGED YUBIKEY PAM$/d' \
  /etc/pam.d/{sudo,lightdm,cinnamon-screensaver,login}
restorecon /etc/pam.d/{sudo,lightdm,cinnamon-screensaver,login}
exit
exit
```

This removes only the role's marked rules, leaves the original password
authentication lines intact, and resumes boot. It does not bypass disk encryption.

Upstream references: [pam-u2f options](https://developers.yubico.com/pam-u2f/Manuals/pam_u2f.8.html)
and [pamu2fcfg enrollment](https://developers.yubico.com/pam-u2f/Manuals/pamu2fcfg.1.html).
