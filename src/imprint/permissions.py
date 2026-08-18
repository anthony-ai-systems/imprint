"""Private local-storage permissions for Imprint-owned state."""

from __future__ import annotations

import os
import json
import shutil
import stat
import subprocess
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _secure_windows_paths(paths: list[Path]) -> None:
    """Set current-user ownership and an exact user/SYSTEM DACL."""
    if not paths:
        return
    executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if executable is None:
        raise OSError("PowerShell is required to secure private Imprint state")
    script = r"""
$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($path in $paths) {
  if (-not (Test-Path -LiteralPath $path)) { continue }  # vanished mid-tighten
  $item = Get-Item -Force -LiteralPath $path
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "refusing reparse-point private state"
  }
  $acl = Get-Acl -LiteralPath $path
  $acl.SetOwner($current)
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
  $inheritance = if ($item.PSIsContainer) {
    [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [Security.AccessControl.InheritanceFlags]::ObjectInherit
  } else { [Security.AccessControl.InheritanceFlags]::None }
  foreach ($allowed in @($current, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $grant = [Security.AccessControl.FileSystemAccessRule]::new(
      $allowed,
      [Security.AccessControl.FileSystemRights]::FullControl,
      $inheritance,
      [Security.AccessControl.PropagationFlags]::None,
      [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($grant)
  }
  Set-Acl -LiteralPath $path -AclObject $acl
}
"""
    result = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        input=json.dumps([str(path) for path in paths]),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise OSError("unable to secure private Imprint state on Windows")


def secure_directory(path: Path) -> Path:
    """Create or tighten an Imprint-owned directory without following links."""
    target = Path(path)
    if target.is_symlink():
        raise OSError(f"refusing symlinked private directory: {target}")
    missing: list[Path] = []
    cursor = target
    while not cursor.exists():
        if cursor.is_symlink():
            raise OSError(f"refusing symlinked private directory: {cursor}")
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    target.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    created = list(reversed(missing))
    candidates = created if created else [target]
    if os.name == "nt":
        _secure_windows_paths(candidates)
    else:
        for candidate in candidates:
            try:
                os.chmod(candidate, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
            except FileNotFoundError:
                continue  # concurrently removed (e.g. a consumed spool claim)
    return target


def secure_file(path: Path) -> Path:
    """Tighten an Imprint-owned regular file.

    A path that no longer exists is a no-op, not an error: SQLite drops its
    -wal/-shm sidecars whenever the last connection closes, racing every
    exists-then-tighten caller, and a vanished file has no modes to leak.
    A path that is PRESENT but irregular (symlink, directory, device) still
    fails closed.
    """
    target = Path(path)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise OSError(f"private file is not a regular file: {target}")
    if os.name == "nt":
        _secure_windows_paths([target])
        return target
    try:
        os.chmod(target, PRIVATE_FILE_MODE, follow_symlinks=False)
    except FileNotFoundError:
        pass
    return target


def secure_tree(root: Path) -> None:
    """Tighten every existing item in an Imprint-owned state tree."""
    base = Path(root)
    if base.is_symlink():
        raise OSError(f"refusing symlinked private directory: {base}")
    base.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if os.name == "nt":
        candidates = [base, *base.rglob("*")]
        if any(path.is_symlink() or not (path.is_dir() or path.is_file()) for path in candidates):
            raise OSError("refusing unsafe private state path")
        _secure_windows_paths(candidates)
        return
    secure_directory(base)
    for current, directories, files in os.walk(base, followlinks=False):
        current_path = Path(current)
        secure_directory(current_path)
        for name in directories:
            secure_directory(current_path / name)
        for name in files:
            secure_file(current_path / name)


def unsafe_posix_permissions(root: Path) -> tuple[str, ...]:
    """Return content-free relative paths that are group/world accessible."""
    if os.name == "nt":
        return ()
    base = Path(root)
    if not base.exists():
        return ()
    unsafe: list[str] = []
    candidates = [base, *base.rglob("*")]
    for path in candidates:
        if path.is_symlink():
            unsafe.append(str(path.relative_to(base)) or ".")
            continue
        if not (path.is_dir() or path.is_file()):
            continue
        mode = _lmode(path)
        if mode is not None and mode & 0o077:
            unsafe.append(str(path.relative_to(base)) or ".")
    return tuple(sorted(set(unsafe)))


def _lmode(path: Path) -> int | None:
    """Permission bits without following links; None if the path vanished
    mid-scan (SQLite sidecars come and go with connection lifetime)."""
    try:
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return None


def unsafe_windows_permissions(root: Path) -> tuple[str, ...]:
    """Return paths granting read/write access beyond the user and SYSTEM."""
    if os.name != "nt":
        return ()
    base = Path(root)
    if not base.exists():
        return ()
    candidates = [base, *base.rglob("*")]
    unsafe = [
        str(path.relative_to(base)) or "."
        for path in candidates
        if path.is_symlink() or not (path.is_dir() or path.is_file())
    ]
    inspectable = [str(path) for path in candidates if path.is_dir() or path.is_file()]
    script = r"""
$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18')
$unsafe = @()
foreach ($path in $paths) {
  if (-not (Test-Path -LiteralPath $path)) { continue }  # vanished mid-scan
  $item = Get-Item -Force -LiteralPath $path
  $acl = Get-Acl -LiteralPath $path
  $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $allowed -notcontains $owner) {
    $unsafe += $path
    continue
  }
  $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
  foreach ($rule in $rules) {
    if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $allowed -notcontains $rule.IdentityReference.Value) {
      $unsafe += $path
      break
    }
  }
}
ConvertTo-Json -Compress -InputObject @($unsafe)
"""
    try:
        executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if executable is None:
            return ("<acl-inspection-failed>",)
        result = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            input=json.dumps(inspectable),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            return ("<acl-inspection-failed>",)
        reported = json.loads(result.stdout or "[]")
        if not isinstance(reported, list) or any(not isinstance(item, str) for item in reported):
            return ("<acl-inspection-failed>",)
        unsafe.extend(str(Path(item).relative_to(base)) or "." for item in reported)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return ("<acl-inspection-failed>",)
    return tuple(sorted(set(unsafe)))


def unsafe_private_permissions(root: Path) -> tuple[str, ...]:
    """Dispatch to the platform's fail-closed private-state permission scan."""
    return unsafe_windows_permissions(root) if os.name == "nt" else unsafe_posix_permissions(root)
