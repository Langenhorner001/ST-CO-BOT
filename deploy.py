import argparse
import base64
import datetime
import io
import os
import posixpath
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path, PurePosixPath


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def load_env_file(path: Path) -> bool:
    if not path.exists():
        return False

    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
        return True
    except Exception as exc:
        print(f"[ENV] .env load skip hua: {exc}")
        return False


load_env_file(ENV_FILE)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def log(msg: str, color: str = RESET) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}")


def run_cmd(
    args: list[str],
    cwd: Path | None = None,
    check: bool = False,
    echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
    )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if echo and stdout:
        print(f"  {stdout}")
    if echo and stderr:
        print(f"  {YELLOW}{stderr}{RESET}")
    if check and result.returncode != 0:
        raise RuntimeError("Command failed: " + " ".join(args))
    return result


DEFAULT_UPLOAD_ITEMS = [
    "file1.py",
    "main.py",
    "scraper.py",
    "shopify_checker.py",
    "database.py",
    "gatet.py",
    "keep_alive.py",
    "deploy.py",
    "stripe_core.py",
    "dlx_hitter.py",
    "ui_formatter.py",
    "requirements.txt",
    "constants",
    "handlers",
    "services",
    "utils",
    "Dlx",
]

DEFAULT_EXCLUDE_PARTS = {
    ".git",
    "__pycache__",
    "attached_assets",
    "backups",
    "node_modules",
    ".venv",
    ".cache",
    ".local",
}

DEFAULT_EXCLUDE_SUFFIXES = {
    ".env",
    ".db",
    ".pyc",
    ".pyo",
    ".zip",
    ".ppk",
    ".pem",
    ".QUARANTINED_UNSAFE",
}


def parse_item_list(raw: str) -> list[str]:
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def resolve_upload_items() -> list[str]:
    items: list[str] = []
    items.extend(DEFAULT_UPLOAD_ITEMS)
    extra = parse_item_list(env("DEPLOY_FILES"))
    for item in extra:
        if item not in items:
            items.append(item)

    excludes = set(parse_item_list(env("DEPLOY_EXCLUDE")))
    if excludes:
        items = [item for item in items if item not in excludes]

    filtered: list[str] = []
    for item in items:
        if item not in filtered:
            filtered.append(item)
    return filtered


def should_skip_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts & DEFAULT_EXCLUDE_PARTS:
        return True
    if path.name.startswith(".") and path.name not in {".replit", ".npmrc"}:
        return True
    if path.suffix in DEFAULT_EXCLUDE_SUFFIXES:
        return True
    if path.name.endswith(".QUARANTINED_UNSAFE"):
        return True
    return False


def git_status_signature() -> str:
    result = run_cmd(["git", "status", "--porcelain=v1", "-uall"], cwd=ROOT, echo=False)
    return (result.stdout or "").strip()


def ensure_git_identity() -> None:
    name = env("GIT_AUTHOR_NAME", "Codex Deploy")
    email = env("GIT_AUTHOR_EMAIL", "codex@local")
    run_cmd(["git", "config", "user.name", name], cwd=ROOT)
    run_cmd(["git", "config", "user.email", email], cwd=ROOT)


def _inject_github_token(remote: str) -> str:
    token = env("GITHUB_TOKEN")
    if not token or not remote:
        return remote
    if not remote.startswith("https://"):
        return remote
    if "@" in remote.split("https://", 1)[1].split("/", 1)[0]:
        return remote
    user = env("GITHUB_USER", "x-access-token")
    return remote.replace("https://", f"https://{user}:{token}@", 1)


def ensure_git_remote() -> str | None:
    remote = env("GITHUB_REMOTE")
    if not remote:
        existing = run_cmd(["git", "remote", "get-url", "origin"], cwd=ROOT, echo=False)
        if existing.returncode == 0 and existing.stdout.strip():
            remote = existing.stdout.strip()

    if not remote:
        return None

    remote = _inject_github_token(remote)

    current = run_cmd(["git", "remote", "get-url", "origin"], cwd=ROOT, echo=False)
    if current.returncode == 0:
        if current.stdout.strip() != remote:
            run_cmd(["git", "remote", "set-url", "origin", remote], cwd=ROOT)
    else:
        run_cmd(["git", "remote", "add", "origin", remote], cwd=ROOT)
    return remote


def git_commit_and_push(commit_msg: str) -> bool:
    if not (ROOT / ".git").exists():
        log("Git repo nahi mila. GitHub push skip.", YELLOW)
        return False

    ensure_git_identity()
    remote = ensure_git_remote()
    branch = env("GITHUB_BRANCH", "main")
    force_push = truthy(env("GITHUB_FORCE_PUSH"))

    status = git_status_signature()
    if not status:
        log("Git working tree clean hai. GitHub push skip.", YELLOW)
        return True

    log("Git changes stage kar raha hoon...", CYAN)
    run_cmd(["git", "add", "-A"], cwd=ROOT, check=True)

    status_after_add = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=ROOT, echo=False)
    if not (status_after_add.stdout or "").strip():
        log("Stage karne ke baad koi change nahi mila.", YELLOW)
        return True

    log(f"Commit: {commit_msg}", CYAN)
    commit = run_cmd(["git", "commit", "-m", commit_msg], cwd=ROOT)
    if commit.returncode != 0:
        log("Git commit fail hua.", RED)
        return False

    if not remote:
        log("GITHUB_REMOTE set nahi hai aur origin bhi missing hai. GitHub push skip.", YELLOW)
        return False

    push_cmd = ["git", "push"]
    if force_push:
        push_cmd.append("--force-with-lease")
    push_cmd.extend(["origin", f"HEAD:refs/heads/{branch}"])

    log(f"GitHub pe push kar raha hoon: {branch}", CYAN)
    push = run_cmd(push_cmd, cwd=ROOT)
    if push.returncode == 0:
        log("GitHub push successful!", GREEN)
        return True

    log("GitHub push fail hua.", RED)
    return False


def _read_blob(data: bytes, offset: int) -> tuple[bytes, int]:
    length = int.from_bytes(data[offset : offset + 4], "big")
    return data[offset + 4 : offset + 4 + length], offset + 4 + length


def _read_mpint(data: bytes, offset: int) -> tuple[int, int]:
    blob, new_offset = _read_blob(data, offset)
    return int.from_bytes(blob, "big"), new_offset


def load_private_key(key_str: str):
    import paramiko

    key_str = key_str.replace("\\n", "\n").strip()
    if not key_str:
        raise ValueError("SSH key blank hai.")

    if key_str.startswith("PuTTY-User-Key-File"):
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.asymmetric.rsa import (
                RSAPrivateNumbers,
                RSAPublicNumbers,
                rsa_crt_dmp1,
                rsa_crt_dmq1,
            )
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
            )

            ppk_lines = key_str.splitlines()
            pub_idx = next(i for i, line in enumerate(ppk_lines) if line.startswith("Public-Lines:"))
            pub_count = int(ppk_lines[pub_idx].split(": ")[1])
            pub_data = base64.b64decode("".join(ppk_lines[pub_idx + 1 : pub_idx + 1 + pub_count]))

            priv_idx = next(i for i, line in enumerate(ppk_lines) if line.startswith("Private-Lines:"))
            priv_count = int(ppk_lines[priv_idx].split(": ")[1])
            priv_data = base64.b64decode("".join(ppk_lines[priv_idx + 1 : priv_idx + 1 + priv_count]))

            _, offset = _read_blob(pub_data, 0)
            e, offset = _read_mpint(pub_data, offset)
            n, offset = _read_mpint(pub_data, offset)

            d, offset = _read_mpint(priv_data, 0)
            p, offset = _read_mpint(priv_data, offset)
            q, offset = _read_mpint(priv_data, offset)
            iqmp, _ = _read_mpint(priv_data, offset)

            dp = rsa_crt_dmp1(d, p)
            dq = rsa_crt_dmq1(d, q)
            pub_nums = RSAPublicNumbers(e, n)
            priv_nums = RSAPrivateNumbers(p, q, d, dp, dq, iqmp, pub_nums)
            priv_key = priv_nums.private_key(default_backend())
            pem = priv_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
            return paramiko.RSAKey.from_private_key(io.StringIO(pem.decode()))
        except Exception as exc:
            raise ValueError(f"PPK key load nahi ho saka: {exc}") from exc

    candidates: list[str]
    if "BEGIN" not in key_str:
        b64 = key_str.replace("\n", "").replace("\r", "").replace(" ", "")
        wrapped = "\n".join(textwrap.wrap(b64, 64))
        candidates = [
            f"-----BEGIN RSA PRIVATE KEY-----\n{wrapped}\n-----END RSA PRIVATE KEY-----\n",
            f"-----BEGIN OPENSSH PRIVATE KEY-----\n{wrapped}\n-----END OPENSSH PRIVATE KEY-----\n",
        ]
    else:
        candidates = [key_str]

    for pem in candidates:
        key_io = io.StringIO(pem)
        for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
            try:
                key_io.seek(0)
                return cls.from_private_key(key_io)
            except Exception:
                continue

    raise ValueError("SSH key load nahi ho saka. Format check karein.")


def load_ssh_key():
    ssh_key = env("EC2_SSH_KEY")
    key_path = env("EC2_SSH_KEY_PATH")
    if not ssh_key and key_path:
        p = Path(key_path).expanduser()
        if p.exists():
            ssh_key = p.read_text(encoding="utf-8", errors="replace")

    if not ssh_key:
        candidate_paths = [
            ROOT / "ec2_key.pem",
            ROOT / "ec2_key.ppk",
            Path.home() / ".ssh" / "ec2_key.pem",
            Path.home() / ".ssh" / "ec2_key.ppk",
        ]
        for candidate in candidate_paths:
            if candidate.exists():
                ssh_key = candidate.read_text(encoding="utf-8", errors="replace")
                log(f"SSH key file se load kiya: {candidate}", CYAN)
                break

    if not ssh_key:
        raise ValueError("SSH key nahi mili. EC2_SSH_KEY ya EC2_SSH_KEY_PATH set karein.")

    return load_private_key(ssh_key)


def remote_mkdirs(sftp, remote_path: str) -> None:
    remote_path = remote_path.replace("\\", "/")
    if not remote_path:
        return

    parts = PurePosixPath(remote_path).parts
    current = "/" if parts and parts[0] == "/" else ""
    for part in parts:
        if part == "/":
            continue
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except (FileNotFoundError, IOError, OSError):
            try:
                sftp.mkdir(current)
            except OSError:
                # Another process may have created it, or the parent may already exist.
                try:
                    sftp.stat(current)
                except Exception:
                    raise


def remote_file_is_current(sftp, local_file: Path, remote_path: str) -> bool:
    if truthy(env("DEPLOY_FORCE_UPLOAD")):
        return False
    try:
        st = sftp.stat(remote_path)
    except (FileNotFoundError, IOError, OSError):
        return False

    local_stat = local_file.stat()
    if int(st.st_size) != int(local_stat.st_size):
        return False

    # We set remote mtime after upload, so this is a cheap and reliable skip
    # for files this deploy script has already synced.
    remote_mtime = int(getattr(st, "st_mtime", 0) or 0)
    local_mtime = int(local_stat.st_mtime)
    return abs(remote_mtime - local_mtime) <= 1


def upload_file(sftp, local_file: Path, remote_path: str, remote_rel: str) -> bool:
    remote_mkdirs(sftp, posixpath.dirname(remote_path))
    if remote_file_is_current(sftp, local_file, remote_path):
        log(f"  Unchanged: {remote_rel}", YELLOW)
        return False

    tmp_path = f"{remote_path}.tmp-{os.getpid()}"
    try:
        sftp.put(str(local_file), tmp_path)
        try:
            sftp.rename(tmp_path, remote_path)
        except OSError:
            try:
                sftp.remove(remote_path)
            except OSError:
                pass
            sftp.rename(tmp_path, remote_path)
        local_mtime = int(local_file.stat().st_mtime)
        try:
            sftp.utime(remote_path, (local_mtime, local_mtime))
        except OSError:
            pass
    except Exception as exc:
        try:
            sftp.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError(f"Upload failed for {remote_rel} -> {remote_path}: {exc}") from exc

    size_kb = local_file.stat().st_size / 1024
    log(f"  Uploaded: {remote_rel} ({size_kb:.1f} KB)", GREEN)
    return True


def upload_item(sftp, local_item: Path, remote_base: str) -> tuple[int, int]:
    uploaded = 0
    skipped = 0
    if local_item.is_dir():
        for child in local_item.rglob("*"):
            if not child.is_file() or should_skip_path(child):
                continue
            remote_rel = child.relative_to(ROOT).as_posix()
            remote_path = f"{remote_base}/{remote_rel}"
            if upload_file(sftp, child, remote_path, remote_rel):
                uploaded += 1
            else:
                skipped += 1
    elif local_item.is_file():
        if should_skip_path(local_item):
            return 0, 0
        remote_rel = local_item.relative_to(ROOT).as_posix()
        remote_path = f"{remote_base}/{remote_rel}"
        if upload_file(sftp, local_item, remote_path, remote_rel):
            uploaded += 1
        else:
            skipped += 1
    return uploaded, skipped


def sync_service_file(client, sftp, remote_root: str) -> bool:
    bot_token = env("BOT_TOKEN")
    admin_id = env("ADMIN_ID")
    service_name = env("EC2_SERVICE", "st-checker-bot")
    start_cmd = env(
        "EC2_START_COMMAND",
        f"{remote_root}/venv/bin/python3 {remote_root}/main.py",
    )

    if not bot_token or not admin_id:
        log("BOT_TOKEN/ADMIN_ID nahi mile. systemd service sync skip.", YELLOW)
        return False

    # Optional env vars — sirf wahi inject hote hain jo set hain
    extra_env_keys = ["RAPIDAPI_KEY", "TG_API_ID", "TG_API_HASH", "TG_SESSION",
                      "API_SECRET_KEY", "BINCHECK_API_KEY", "STRIPE_KEY_ENCRYPTION_SECRET"]
    extra_env_lines = ""
    for k in extra_env_keys:
        v = env(k)
        if v:
            extra_env_lines += f'Environment="{k}={v}"\n'

    service_path = f"/etc/systemd/system/{service_name}.service"
    tmp_path = f"/tmp/{service_name}.service"
    content = (
        "[Unit]\n"
        "Description=ST-CHECKER Telegram Bot\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={env('EC2_USER', 'ubuntu')}\n"
        f"WorkingDirectory={remote_root}\n"
        f'Environment="BOT_TOKEN={bot_token}"\n'
        f'Environment="ADMIN_ID={admin_id}"\n'
        f"{extra_env_lines}"
        f"ExecStart={start_cmd}\n"
        "Restart=always\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    with sftp.file(tmp_path, "w") as fh:
        fh.write(content)

    cmd = f"sudo mv {shlex.quote(tmp_path)} {shlex.quote(service_path)} && sudo systemctl daemon-reload"
    stdin, stdout, stderr = client.exec_command(cmd, get_pty=False)
    stdout.channel.recv_exit_status()
    log("Service file updated with fresh BOT_TOKEN & ADMIN_ID.", GREEN)
    return True


def install_remote_deps(client, remote_root: str) -> None:
    req_path = f"{remote_root}/requirements.txt"
    pip_install = (
        f"if [ -x {shlex.quote(f'{remote_root}/venv/bin/pip')} ]; then "
        f"{shlex.quote(f'{remote_root}/venv/bin/pip')} install -r {shlex.quote(req_path)} -q; "
        "else python3 -m pip install -r "
        f"{shlex.quote(req_path)} -q; "
        "fi"
    )
    log("Dependencies install/update chal raha hai...", CYAN)
    stdin, stdout, stderr = client.exec_command(pip_install, get_pty=False)
    code = stdout.channel.recv_exit_status()
    err = (stderr.read().decode(errors="replace") or "").strip()
    if code == 0:
        log("Dependencies updated.", GREEN)
    else:
        log(f"pip warning (non-fatal): {err[:180]}", YELLOW)


def restart_remote_service(client, service_name: str) -> bool:
    log(f"Bot restart kar raha hoon: {service_name}", CYAN)
    stdin, stdout, stderr = client.exec_command(f"sudo systemctl restart {shlex.quote(service_name)}", get_pty=False)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if out:
        print(f"  {out}")
    if err:
        print(f"  {YELLOW}{err}{RESET}")
    return exit_code == 0


def deploy_to_ec2() -> bool:
    host = env("EC2_HOST")
    deploy_path = env("EC2_DEPLOY_PATH")
    user = env("EC2_USER", "ubuntu")

    if not host:
        log("EC2_HOST set nahi hai. Deploy skip.", RED)
        return False
    if not deploy_path:
        log("EC2_DEPLOY_PATH set nahi hai. Deploy skip.", RED)
        return False

    try:
        import paramiko
    except ImportError:
        log("paramiko library nahi mili. pip install paramiko chahiye.", RED)
        return False

    try:
        pkey = load_ssh_key()
    except Exception as exc:
        log(f"SSH key error: {exc}", RED)
        return False

    service_name = env("EC2_SERVICE", "st-checker-bot")
    upload_items = resolve_upload_items()

    log(f"EC2 se connect kar raha hoon: {user}@{host}", CYAN)
    log(f"Deploy path: {deploy_path}", CYAN)

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username=user,
            pkey=pkey,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
        log("SSH connection kamyab!", GREEN)

        remote_root = deploy_path.rstrip("/")
        sftp = client.open_sftp()
        remote_mkdirs(sftp, remote_root)
        uploaded = 0
        skipped = 0

        for item in upload_items:
            local_item = ROOT / item
            if not local_item.exists():
                log(f"  Skip (nahi mila): {item}", YELLOW)
                continue
            item_uploaded, item_skipped = upload_item(sftp, local_item, remote_root)
            uploaded += item_uploaded
            skipped += item_skipped

        sftp.close()
        log(f"{uploaded} files uploaded, {skipped} unchanged skipped.", CYAN)

        if uploaded == 0 and not truthy(env("DEPLOY_RESTART_ALWAYS")):
            log("Remote files already current hain. Service sync/deps/restart skip.", GREEN)
            client.close()
            return True

        sftp = client.open_sftp()
        sync_service_file(client, sftp, remote_root)
        install_remote_deps(client, remote_root)
        restarted = restart_remote_service(client, service_name)
        sftp.close()
        client.close()

        if restarted:
            log("EC2 deploy successful! Bot restart ho gaya.", GREEN)
            return True

        log("EC2 restart fail hua.", RED)
        return False

    except Exception as exc:
        log(f"EC2 connection error: {exc}", RED)
        return False


def deploy_once(commit_msg: str, skip_github: bool, skip_ec2: bool) -> tuple[bool | None, bool]:
    github_ok: bool | None
    if skip_github:
        log("GitHub push: skipped.", YELLOW)
        github_ok = None
    else:
        github_ok = git_commit_and_push(commit_msg)

    ec2_ok = True
    if skip_ec2:
        log("EC2 deploy: skipped.", YELLOW)
    else:
        ec2_ok = deploy_to_ec2()

    return github_ok, ec2_ok


def watch_loop(interval: int, skip_github: bool, skip_ec2: bool, quiet_period: int = 15) -> None:
    """Watch repo and auto-deploy on changes.

    Debounce: after a change is detected, wait until the working tree has been
    'quiet' (no new changes) for `quiet_period` seconds before deploying. This
    bundles bursts of edits into a single deploy/restart cycle.
    """
    log(f"Watch mode start ho gaya. Interval: {interval}s, Debounce: {quiet_period}s", CYAN)
    pending_sig = None
    pending_since = 0.0

    while True:
        sig = git_status_signature()
        now = time.time()

        if sig:
            if sig != pending_sig:
                # New / changed edits — reset the debounce timer
                pending_sig = sig
                pending_since = now
                log(f"Change detect hua. {quiet_period}s quiet-period ka intezar...", YELLOW)
            elif now - pending_since >= quiet_period:
                # Working tree has been stable for the full quiet period — deploy
                commit_msg = f"Auto deploy - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                log("Quiet period mukammal. Deploy chal raha hai...", CYAN)
                github_ok, ec2_ok = deploy_once(commit_msg, skip_github, skip_ec2)
                print()
                print(f"{BOLD}{CYAN}Deploy summary{RESET}")
                print(f"  GitHub : {GREEN + 'Success' if github_ok else YELLOW + 'Skipped/Failed' if github_ok is not None else YELLOW + 'Skipped'}{RESET}")
                print(f"  EC2    : {GREEN + 'Success' if ec2_ok else RED + 'Failed'}{RESET}")
                print()
                pending_sig = None
                pending_since = 0.0
        else:
            # Working tree is clean — clear any pending state
            pending_sig = None
            pending_since = 0.0

        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub + EC2 deploy helper")
    parser.add_argument("commit_message", nargs="?", default="", help="Git commit message")
    parser.add_argument("--watch", action="store_true", help="Watch repo and auto deploy on changes")
    parser.add_argument("--skip-github", action="store_true", help="Skip GitHub push")
    parser.add_argument("--skip-ec2", action="store_true", help="Skip EC2 deploy")
    parser.add_argument("--interval", type=int, default=int(env("DEPLOY_WATCH_INTERVAL", "5") or 5), help="Watch interval in seconds")
    parser.add_argument("--quiet-period", type=int, default=int(env("DEPLOY_QUIET_PERIOD", "15") or 15), help="Debounce window: wait N seconds of no changes before deploying")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    commit_msg = args.commit_message or f"Update - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    print(f"\n{BOLD}{GREEN}==================================")
    print("    DEPLOY SCRIPT")
    print("==================================")
    print(f"{RESET}")
    print(f"{CYAN}GitHub branch: {env('GITHUB_BRANCH', 'main')}{RESET}")
    print(f"{CYAN}EC2 host     : {env('EC2_HOST', '(not set)')}{RESET}")
    print(f"{CYAN}EC2 path     : {env('EC2_DEPLOY_PATH', '(not set)')}{RESET}")
    print(f"{CYAN}Commit msg   : \"{commit_msg}\"{RESET}")

    if args.watch:
        watch_loop(args.interval, args.skip_github, args.skip_ec2, args.quiet_period)
        return

    github_ok, ec2_ok = deploy_once(commit_msg, args.skip_github, args.skip_ec2)

    print(f"\n{BOLD}{CYAN}DEPLOY SUMMARY{RESET}")
    if github_ok is None:
        print(f"  GitHub : {YELLOW}Skipped{RESET}")
    else:
        print(f"  GitHub : {GREEN + 'Success' if github_ok else RED + 'Failed'}{RESET}")
    print(f"  EC2    : {GREEN + 'Success' if ec2_ok else RED + 'Failed'}{RESET}")

    if not ec2_ok and not args.skip_ec2:
        sys.exit(1)


if __name__ == "__main__":
    main()
