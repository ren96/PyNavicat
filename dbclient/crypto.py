"""本地密码加密。

优先级：
1. 系统钥匙串（keyring）——最安全，见 connection.py
2. 本模块的 Fernet 加密——密钥 = PBKDF2(机器指纹 + 本地随机密钥, 随机盐)
3. cryptography 不可用时退回 base64 混淆（仅兼容旧数据）

落盘格式：
    "v2:<fernet_token>"   —— 新版加密
    "<base64_string>"     —— 旧版混淆（兼容读取，不再写入）
"""
from __future__ import annotations

import base64
import getpass
import logging
import os
import platform
import uuid
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception
    _HAS_CRYPTO = False

log = logging.getLogger(__name__)

_SALT_FILE = "secrets.salt"
_LOCAL_KEY_FILE = "secrets.key"
_PREFIX = "v2:"
_PBKDF2_ITERS = 200_000


def _machine_fingerprint() -> bytes:
    """尽量稳定的机器身份：用户名 + 主机名 + 系统 + 架构 + MAC。"""
    parts = [
        _safe(getpass.getuser),
        _safe(platform.node),
        _safe(platform.system),
        _safe(platform.machine),
        _safe(lambda: str(uuid.getnode())),
    ]
    return "|".join(parts).encode("utf-8")


def _safe(fn) -> str:
    try:
        return str(fn()) or ""
    except Exception:  # noqa: BLE001
        return ""


class LocalCipher:
    """机器绑定的本地加密器。"""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self._fernet: "Fernet | None" = None
        if _HAS_CRYPTO:
            try:
                self._fernet = self._build_fernet()
            except Exception as exc:  # noqa: BLE001
                log.warning("本地加密初始化失败，将使用 base64 兜底：%s", exc)
                self._fernet = None

    # ---------- 是否真正加密 ----------
    def available(self) -> bool:
        return self._fernet is not None

    # ---------- 内部 ----------
    def _build_fernet(self) -> "Fernet":
        salt = self._load_or_create(_SALT_FILE, 16)
        random_key = self._load_or_create(_LOCAL_KEY_FILE, 32)
        material = _machine_fingerprint() + random_key

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERS,
        )
        return Fernet(base64.urlsafe_b64encode(kdf.derive(material)))

    def _load_or_create(self, name: str, size: int) -> bytes:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_dir / name
        if path.exists():
            try:
                data = base64.b64decode(path.read_bytes())
                if len(data) == size:
                    return data
                log.warning("%s 长度异常，重新生成", path)
            except Exception:  # noqa: BLE001
                log.warning("%s 损坏，重新生成", path)
        data = os.urandom(size)
        path.write_bytes(base64.b64encode(data))

        if os.name == "nt":
            # Windows 下 os.chmod 几乎不生效，访问控制由用户目录 ACL 决定
            log.debug("Windows 下凭据文件 %s 的访问控制依赖目录 ACL", path)
        else:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return data

    # ---------- 对外接口 ----------
    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        if self._fernet is not None:
            token = self._fernet.encrypt(plaintext.encode("utf-8"))
            return _PREFIX + token.decode("ascii")
        # 兜底：不写明文，但强度有限
        log.warning("cryptography 不可用，密码将以 base64 混淆存储")
        return base64.b64encode(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, stored: str) -> str:
        if not stored:
            return ""
        if stored.startswith(_PREFIX):
            if self._fernet is None:
                log.warning("密文为 v2 格式，但本机 cryptography 不可用")
                return ""
            try:
                return self._fernet.decrypt(
                    stored[len(_PREFIX):].encode("ascii")
                ).decode("utf-8")
            except InvalidToken:
                log.warning("密码解密失败（可能换了机器/用户，或 secrets.key 丢失）")
                return ""
            except Exception as exc:  # noqa: BLE001
                log.warning("密码解密异常：%s", exc)
                return ""
        # 旧版本 base64 兼容
        try:
            return base64.b64decode(stored.encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return ""