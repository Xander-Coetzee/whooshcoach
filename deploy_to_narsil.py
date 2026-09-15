import os
import sys
import time
from pathlib import Path
import paramiko

import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

LOCAL_ROOT = Path(__file__).resolve().parent

# Load deployment credentials from environment or gitignored .deploy_secrets.json
secrets = {}
secrets_file = LOCAL_ROOT / ".deploy_secrets.json"
if secrets_file.exists():
    try:
        with open(secrets_file, "r", encoding="utf-8") as f:
            secrets = json.load(f)
    except Exception:
        pass

HOST = os.environ.get("NARSIL_HOST", secrets.get("host", "narsil"))
USER = os.environ.get("NARSIL_USER", secrets.get("username", "xcoetzee"))
PASS = os.environ.get("NARSIL_PASSWORD", secrets.get("password", ""))
REMOTE_DIR = os.environ.get("NARSIL_REMOTE_DIR", secrets.get("remote_dir", "/home/xcoetzee/docker/whooshcoach"))

def ensure_remote_dir(sftp, remote_path):
    """Recursively creates remote directories over SFTP."""
    dirs = remote_path.strip("/").split("/")
    current = ""
    for d in dirs:
        current += "/" + d
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except Exception:
                pass

def upload_directory(sftp, local_dir, remote_dir):
    """Recursively uploads a local directory to remote over SFTP."""
    ensure_remote_dir(sftp, remote_dir)
    for root, dirs, files in os.walk(local_dir):
        rel_path = os.path.relpath(root, local_dir)
        target_dir = remote_dir if rel_path == "." else f"{remote_dir}/{rel_path}".replace("\\", "/")
        ensure_remote_dir(sftp, target_dir)
        for f in files:
            local_file = os.path.join(root, f)
            remote_file = f"{target_dir}/{f}".replace("\\", "/")
            print(f"  Uploading {os.path.relpath(local_file, LOCAL_ROOT)} -> {remote_file}...")
            sftp.put(local_file, remote_file)

def deploy():
    print("=" * 60)
    print(f"DEPLOYING WHOOSHCOACH TO {HOST.upper()}")
    print("=" * 60)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print(f"Connecting to {HOST} as {USER}...")
        client.connect(HOST, username=USER, password=PASS, timeout=15)
        print(f"Successfully connected to {HOST}!\n")

        sftp = client.open_sftp()

        # 1. Create remote target directory
        print(f"Ensuring target directory {REMOTE_DIR} exists...")
        ensure_remote_dir(sftp, REMOTE_DIR)
        ensure_remote_dir(sftp, f"{REMOTE_DIR}/data")

        # 2. Upload root project files
        root_files = [
            "Dockerfile",
            "docker-compose.yml",
            "requirements.txt",
            "mywhoosh_workouts_unique.csv",
            ".env.example"
        ]

        print("\nUploading project definition files...")
        for fname in root_files:
            lpath = LOCAL_ROOT / fname
            if lpath.exists():
                rpath = f"{REMOTE_DIR}/{fname}"
                print(f"  Uploading {fname}...")
                sftp.put(str(lpath), rpath)
            else:
                print(f"  Warning: Local file {fname} not found!")

        # 3. Clean any legacy remote files and upload app directory
        print("\nCleaning legacy remote files if any...")
        client.exec_command(f"rm -f {REMOTE_DIR}/app/strava_service.py")
        print("Uploading app/ source tree...")
        upload_directory(sftp, str(LOCAL_ROOT / "app"), f"{REMOTE_DIR}/app")

        sftp.close()
        print("\nAll files successfully transferred via SFTP!\n")

        # 4. Build and run with Docker Compose
        build_cmd = f"cd {REMOTE_DIR} && docker compose down && docker compose up -d --build"
        print(f"Executing remote Docker build and startup:\n  $ {build_cmd}\n")

        stdin, stdout, stderr = client.exec_command(build_cmd, get_pty=True)
        
        # Stream remote output
        for line in iter(stdout.readline, ""):
            print(line, end="")

        exit_status = stdout.channel.recv_exit_status()
        print(f"\nDocker compose command exited with status: {exit_status}\n")

        if exit_status != 0:
            print("Deployment encountered an error during docker compose up.")
            client.close()
            return False

        # 5. Verify container health
        print("Verifying running containers...")
        stdin, stdout, stderr = client.exec_command("docker ps --filter 'name=whooshcoach' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'")
        ps_out = stdout.read().decode().strip()
        print(ps_out)

        # 6. Test HTTP endpoints on narsil
        time.sleep(3)
        print("\nTesting local HTTP endpoint on narsil...")
        stdin, stdout, stderr = client.exec_command("curl -s http://localhost:8555/api/settings")
        settings_out = stdout.read().decode().strip()
        print(f"Settings API query response: {settings_out}")

        stdin, stdout, stderr = client.exec_command("curl -s http://localhost:8555/api/races")
        races_out = stdout.read().decode().strip()
        print(f"Races API query response: {races_out}")

        stdin, stdout, stderr = client.exec_command("curl -s http://localhost:8555/api/workouts?limit=1")
        test_workouts = stdout.read().decode().strip()
        print(f"Sample workouts API query response: {test_workouts[:120]}...\n")

        client.close()

        print("=" * 60)
        print("DEPLOYMENT SUCCESSFUL!")
        print("=" * 60)
        print(f"Access your WhooshCoach app in your browser at:")
        print(f"  • Local Network:  http://192.168.0.55:8555")
        print(f"  • Local Hostname: http://narsil:8555")
        print(f"  • Tailscale VPN:  http://100.124.29.68:8555")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"Deployment failed with exception: {e}")
        return False

if __name__ == '__main__':
    deploy()
