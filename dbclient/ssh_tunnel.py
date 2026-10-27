"""SSH 隧道封装（可选依赖 sshtunnel）。"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

try:
    from sshtunnel import SSHTunnelForwarder
    _HAS = True
except ImportError:
    SSHTunnelForwarder = None
    _HAS = False


def available() -> bool:
    return _HAS


def _explain(exc: BaseException) -> str:
    """把 paramiko / socket 的异常文本转成简短中文。"""
    text = str(exc).strip() or exc.__class__.__name__
    low = text.lower()
    if "authentication failed" in low or "auth fail" in low:
        return f"认证失败（{text}）"
    if "no acceptable authentication" in low:
        return f"服务器拒绝了认证方式（{text}）"
    if "agent" in low and ("no keys" in low or "could not" in low):
        return f"ssh-agent 中没有可用密钥（{text}）"
    if "pageant" in low:
        return f"Pageant 不可用或未加载密钥（{text}）"
    if "connection refused" in low:
        return f"连接被拒绝（{text}）"
    if "timed out" in low or "timeout" in low:
        return f"连接超时（{text}）"
    if "no route to host" in low:
        return f"主机不可达（{text}）"
    if "getaddrinfo" in low or "name or service not known" in low:
        return f"主机名无法解析（{text}）"
    if "private key" in low or "pkey" in low:
        return f"私钥不可用（{text}）"
    return f"隧道建立失败（{text}）"


class SshTunnel:
    """为一条连接打开的 SSH 隧道，本地端口由系统分配。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._server: Any = None
        self.local_host = ""
        self.local_port = 0

    @property
    def active(self) -> bool:
        return self._server is not None and getattr(self._server, "is_active", False)

    def open(self) -> tuple[str, int]:
        # 用 "SSH:" 前缀标记 SSH 阶段错误，对话框据此翻译。
        if not _HAS:
            raise RuntimeError(
                "SSH: 未安装 sshtunnel 库，无法建立 SSH 隧道。"
                "请执行：pip install sshtunnel"
            )
        if not self.cfg.ssh_host:
            raise RuntimeError("SSH: SSH 主机未配置")

        kwargs: dict[str, Any] = {
            "ssh_address_or_host": (self.cfg.ssh_host, int(self.cfg.ssh_port or 22)),
            "ssh_username": self.cfg.ssh_user or None,
            "remote_bind_address": (self.cfg.host, int(self.cfg.port or 3306)),
            "local_bind_address": ("127.0.0.1", 0),
        }

        # 认证优先级：agent > 私钥 > 密码。
        # 勾选「使用 ssh-agent」时，明确不传 password/pkey，让 paramiko
        # 走 agent（Linux/macOS: ssh-add；Windows: Pageant 或系统 ssh-agent）。
        if getattr(self.cfg, "ssh_use_agent", False):
            kwargs["allow_agent"] = True
        elif self.cfg.ssh_key_file:
            kwargs["ssh_pkey"] = self.cfg.ssh_key_file
        elif self.cfg.ssh_password:
            kwargs["ssh_password"] = self.cfg.ssh_password

        server = SSHTunnelForwarder(**kwargs)
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001
            # 启动失败时把 sshtunnel 内部可能已分配的资源也释放掉。
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("SSH: " + _explain(exc)) from exc

        self._server = server
        self.local_host = server.local_bind_host
        self.local_port = int(server.local_bind_port)
        log.info(
            "SSH 隧道已建立：%s:%s → %s:%s（本地 %s:%s）",
            self.cfg.ssh_host, self.cfg.ssh_port,
            self.cfg.host, self.cfg.port,
            self.local_host, self.local_port,
        )
        return self.local_host, self.local_port

    def close(self) -> None:
        if self._server is None:
            return
        try:
            self._server.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("关闭 SSH 隧道失败：%s", exc)
        finally:
            self._server = None
            self.local_host = ""
            self.local_port = 0