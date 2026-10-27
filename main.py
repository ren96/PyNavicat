"""应用入口：初始化样式、日志、主窗口。"""
from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from dbclient.config import APP_NAME, APP_ORG, app_icon, settings
from dbclient.logging_setup import init_logging
from dbclient.main_window import MainWindow
from dbclient.theme import Theme, theme_manager

log = logging.getLogger(__name__)

# 任务栏图标分组 ID（Windows 任务栏据此识别应用）
APP_USER_MODEL_ID = "PyCatStudio.PyNavicat"


def _set_windows_app_id() -> None:
    """Windows 任务栏图标依赖 AppUserModelID；必须在 QApplication 创建前调用。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("设置 AppUserModelID 失败：%s", exc)


def _install_excepthook() -> None:
    """未捕获异常写日志并弹窗提示。"""

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error("未捕获异常:\n%s", text)
        try:
            QMessageBox.critical(None, "程序异常", str(exc_value))
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def main() -> int:
    # 必须在 QApplication 创建前设置，否则任务栏图标不生效
    _set_windows_app_id()

    init_logging()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setStyle("Fusion")

    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    s = settings()

    # 从设置恢复主题
    saved = s.value("ui/theme", "light")
    theme = Theme.DARK if str(saved).lower() == "dark" else Theme.LIGHT
    theme_manager.apply(app, theme)

    _install_excepthook()

    window = MainWindow()
    window.show()
    # 让窗口先完整绘制一帧，再进入事件循环处理延迟启动任务
    # （keyring 探测 / 页签恢复都在事件循环里触发）。
    QApplication.processEvents()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())