# Windows OpenSSH Server Setup

This note shows how to check whether a Windows PC has an SSH server running, how to install it, and how to interpret a typical `sshd_config` file.

## Check Whether OpenSSH Server Is Installed

Open PowerShell as Administrator and run:

```powershell
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
```

Look for:

```text
State : Installed
```

Then check whether the SSH server service exists and is running:

```powershell
Get-Service sshd
```

If SSH is running, the `Status` value should be:

```text
Running
```

You can also test whether SSH port `22` is listening:

```powershell
Test-NetConnection -ComputerName localhost -Port 22
```

If SSH is reachable locally, look for:

```text
TcpTestSucceeded : True
```

## Install OpenSSH Server

Open PowerShell as Administrator and run:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

Start the SSH server:

```powershell
Start-Service sshd
```

Enable it to start automatically after reboot:

```powershell
Set-Service -Name sshd -StartupType Automatic
```

Allow SSH through Windows Firewall:

```powershell
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

## Connect To The Windows PC

From another computer:

```bash
ssh your_windows_username@windows_pc_ip
```

Find the Windows PC's IP address with:

```powershell
ipconfig
```

Look for the IPv4 address, usually something like:

```text
192.168.x.x
```

## Useful Troubleshooting Commands

Check SSH server status:

```powershell
Get-Service sshd
```

Restart SSH server:

```powershell
Restart-Service sshd
```

Open the SSH server config file:

```powershell
notepad C:\ProgramData\ssh\sshd_config
```

After changing `sshd_config`, restart SSH:

```powershell
Restart-Service sshd
```

## Example `sshd_config`

This is a normal/default-looking Windows OpenSSH server configuration:

```text
# This is the sshd server system-wide configuration file.  See
# sshd_config(5) for more information.

# The strategy used for options in the default sshd_config shipped with
# OpenSSH is to specify options with their default value where
# possible, but leave them commented.  Uncommented options override the
# default value.

#Port 22
#AddressFamily any
#ListenAddress 0.0.0.0
#ListenAddress ::

#HostKey __PROGRAMDATA__/ssh/ssh_host_rsa_key
#HostKey __PROGRAMDATA__/ssh/ssh_host_dsa_key
#HostKey __PROGRAMDATA__/ssh/ssh_host_ecdsa_key
#HostKey __PROGRAMDATA__/ssh/ssh_host_ed25519_key

# Ciphers and keying
#RekeyLimit default none

# Logging
#SyslogFacility AUTH
#LogLevel INFO

# Authentication:

#LoginGraceTime 2m
#PermitRootLogin prohibit-password
#StrictModes yes
#MaxAuthTries 6
#MaxSessions 10

#PubkeyAuthentication yes

# The default is to check both .ssh/authorized_keys and .ssh/authorized_keys2
# but this is overridden so installations will only check .ssh/authorized_keys
AuthorizedKeysFile	.ssh/authorized_keys

#AuthorizedPrincipalsFile none

# For this to work you will also need host keys in %programData%/ssh/ssh_known_hosts
#HostbasedAuthentication no
# Change to yes if you don't trust ~/.ssh/known_hosts for
# HostbasedAuthentication
#IgnoreUserKnownHosts no
# Don't read the user's ~/.rhosts and ~/.shosts files
#IgnoreRhosts yes

# To disable tunneled clear text passwords, change to no here!
#PasswordAuthentication yes
#PermitEmptyPasswords no

# GSSAPI options
#GSSAPIAuthentication no

#AllowAgentForwarding yes
#AllowTcpForwarding yes
#GatewayPorts no
#PermitTTY yes
#PrintMotd yes
#PrintLastLog yes
#TCPKeepAlive yes
#UseLogin no
#PermitUserEnvironment no
#ClientAliveInterval 0
#ClientAliveCountMax 3
#UseDNS no
#PidFile /var/run/sshd.pid
#MaxStartups 10:30:100
#PermitTunnel no
#ChrootDirectory none
#VersionAddendum none

# no default banner path
#Banner none

# override default of no subsystems
Subsystem	sftp	sftp-server.exe

# Example of overriding settings on a per-user basis
#Match User anoncvs
#	AllowTcpForwarding no
#	PermitTTY no
#	ForceCommand cvs server

Match Group administrators
       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
```

## Notes About This Config

This file is generally OK.

SSH listens on port `22` by default because this line is commented, but `22` is still the default:

```text
#Port 22
```

Password login is probably enabled by default because this line is commented with the default value:

```text
#PasswordAuthentication yes
```

SFTP is enabled:

```text
Subsystem	sftp	sftp-server.exe
```

For normal non-admin users, SSH public keys go here:

```text
C:\Users\<username>\.ssh\authorized_keys
```

For users in the Windows `Administrators` group, this config uses:

```text
C:\ProgramData\ssh\administrators_authorized_keys
```

That last part often surprises people. If you are trying to log in as an admin account using SSH keys, putting the key in your user profile's `.ssh\authorized_keys` may not work unless you remove or comment this block:

```text
Match Group administrators
       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
```

If password login is enough, this config probably does not need to be changed.
