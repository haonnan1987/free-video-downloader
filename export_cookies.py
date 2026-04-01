"""
一键导出 Chrome YouTube Cookie 到 Netscape cookies.txt 格式。

用法：
  1. 完全关闭 Chrome（任务栏、后台都退出）
  2. 运行：  .\.venv\Scripts\python.exe export_cookies.py
  3. 成功后重启 uvicorn，即可解析 YouTube
  4. 可以重新打开 Chrome
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        import win32crypt
    except ImportError:
        win32crypt = None
    try:
        from Crypto.Cipher import AES
    except ImportError:
        AES = None
else:
    win32crypt = None
    AES = None

PROJECT_ROOT = Path(__file__).resolve().parent
COOKIES_TXT = PROJECT_ROOT / "cookies.txt"

def find_chrome_cookies() -> Path | None:
    if sys.platform != "win32":
        print("当前仅支持 Windows")
        return None
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        local / "Google" / "Chrome" / "User Data" / "Default" / "Network" / "Cookies",
        local / "Google" / "Chrome" / "User Data" / "Default" / "Cookies",
        local / "Google" / "Chrome" / "User Data" / "Profile 1" / "Network" / "Cookies",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def get_chrome_key() -> bytes | None:
    """Read Chrome's AES-GCM encryption key from Local State."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    local_state = local / "Google" / "Chrome" / "User Data" / "Local State"
    if not local_state.is_file():
        return None
    import json
    data = json.loads(local_state.read_text(encoding="utf-8"))
    encrypted_key_b64 = data.get("os_crypt", {}).get("encrypted_key")
    if not encrypted_key_b64:
        return None
    import base64
    encrypted_key = base64.b64decode(encrypted_key_b64)
    if encrypted_key[:5] != b"DPAPI":
        return None
    if win32crypt is None:
        print("警告：未安装 pywin32，无法解密 v10+ cookie（pip install pywin32）")
        return None
    try:
        return win32crypt.CryptUnprotectData(encrypted_key[5:], None, None, None, 0)[1]
    except Exception as e:
        print(f"DPAPI 解密 Chrome key 失败: {e}")
        return None


def decrypt_cookie_value(encrypted: bytes, key: bytes | None) -> str:
    if not encrypted:
        return ""
    if encrypted[:3] == b"v10" or encrypted[:3] == b"v20":
        if key and AES:
            try:
                nonce = encrypted[3:15]
                ciphertext = encrypted[15:]
                cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
                return cipher.decrypt_and_verify(ciphertext[:-16], ciphertext[-16:]).decode("utf-8", errors="replace")
            except Exception:
                pass
        if win32crypt:
            try:
                return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8", errors="replace")
            except Exception:
                pass
        return ""
    if win32crypt:
        try:
            return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8", errors="replace")
        except Exception:
            pass
    return encrypted.decode("utf-8", errors="replace") if encrypted else ""


def export():
    cookie_db = find_chrome_cookies()
    if not cookie_db:
        print("未找到 Chrome cookie 数据库文件。")
        print("请确认已安装 Chrome 并至少打开过一次。")
        return False

    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    try:
        shutil.copy2(cookie_db, tmp)
    except PermissionError:
        print("无法复制 cookie 数据库 — Chrome 可能仍在运行！")
        print("请完全关闭 Chrome（包括后台进程），然后重新运行此脚本。")
        print("  提示：Ctrl+Shift+Esc 打开任务管理器，搜 chrome，全部结束。")
        return False

    key = get_chrome_key()

    conn = sqlite3.connect(str(tmp))
    cursor = conn.cursor()
    try:
        rows = cursor.execute(
            "SELECT host_key, name, path, is_secure, expires_utc, encrypted_value "
            "FROM cookies WHERE host_key LIKE '%youtube.com%' OR host_key LIKE '%google.com%'"
        ).fetchall()
    except Exception as e:
        print(f"读取 cookie 数据库失败: {e}")
        conn.close()
        return False
    conn.close()
    tmp.unlink(missing_ok=True)

    if not rows:
        print("Chrome 中没有 YouTube/Google cookie。")
        print("请先用 Chrome 打开 youtube.com 并登录，然后关闭 Chrome 再运行此脚本。")
        return False

    lines = ["# Netscape HTTP Cookie File", "# Exported by export_cookies.py", ""]
    count = 0
    for host, name, path, secure, expires, enc_val in rows:
        value = decrypt_cookie_value(enc_val, key)
        if not value:
            continue
        secure_str = "TRUE" if secure else "FALSE"
        domain_flag = "TRUE" if host.startswith(".") else "FALSE"
        exp = str(int(expires / 1_000_000 - 11644473600)) if expires > 0 else "0"
        lines.append(f"{host}\t{domain_flag}\t{path}\t{secure_str}\t{exp}\t{name}\t{value}")
        count += 1

    if count == 0:
        print("解密得到 0 条有效 cookie（DPAPI 可能失败）。")
        return False

    COOKIES_TXT.write_text("\n".join(lines), encoding="utf-8")
    print(f"成功导出 {count} 条 cookie 到 {COOKIES_TXT}")

    env_file = PROJECT_ROOT / ".env"
    env_text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    if "YTDLP_COOKIES_FILE" not in env_text:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"\nYTDLP_COOKIES_FILE={COOKIES_TXT}\n")
        print(f".env 已更新：YTDLP_COOKIES_FILE={COOKIES_TXT}")
    if "YTDLP_COOKIES_FROM_BROWSER" in env_text:
        new_text = "\n".join(
            line for line in env_text.splitlines()
            if not line.strip().startswith("YTDLP_COOKIES_FROM_BROWSER")
        )
        env_file.write_text(new_text.strip() + "\n", encoding="utf-8")
        print("已从 .env 移除 YTDLP_COOKIES_FROM_BROWSER（改用文件模式）")

    print("\n下一步：重启 uvicorn 服务，然后测试 YouTube 链接。")
    return True


if __name__ == "__main__":
    export()
