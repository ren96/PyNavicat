"""结果表单元格编辑委托：按列的 SQL 类型分派到合适的编辑器。"""
from __future__ import annotations

import re
from datetime import date, datetime, time

from PySide6.QtCore import QDate, QDateTime, QRegularExpression, QTime, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QDateTimeEdit, QDoubleSpinBox, QLineEdit,
    QStyledItemDelegate, QTimeEdit,
)

_INT_RE = re.compile(
    r"^(int|integer|bigint|smallint|tinyint|mediumint|serial|bigserial)",
    re.I,
)
_FLOAT_RE = re.compile(
    r"^(decimal|numeric|float|double|real|money|number"
    r"|binary_float|binary_double)",
    re.I,
)
_BOOL_RE = re.compile(r"^(bool|boolean|bit)", re.I)
_DATETIME_RE = re.compile(
    r"^(datetime|timestamp|timestamptz|smalldatetime|datetime2)", re.I
)
_DATE_RE = re.compile(r"^date$", re.I)
_TIME_RE = re.compile(r"^time$", re.I)

_INT_TEXT_RE = QRegularExpression(r"^[+-]?\d{0,19}$")
_FLOAT_TEXT_RE = QRegularExpression(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

# 编辑器与单元格上下边框的缓冲像素；用于 updateEditorGeometry。
# 太小（0）在高 DPI 缩放下文字会贴边被截断；太大又会让编辑器显得悬空。
_EDITOR_V_MARGIN = 2


def classify(type_name: str | None) -> str:
    if not type_name:
        return "text"
    name = str(type_name).strip()
    if _BOOL_RE.match(name):
        return "bool"
    if _DATETIME_RE.match(name):
        return "datetime"
    if _DATE_RE.match(name):
        return "date"
    if _TIME_RE.match(name):
        return "time"
    if _INT_RE.match(name):
        return "int"
    if _FLOAT_RE.match(name):
        return "float"
    return "text"


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(value[:10], fmt).date()
            except ValueError:
                continue
    return None


def _parse_time(value):
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
    return None


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
    return None


class TypedEditDelegate(QStyledItemDelegate):
    """根据列的 SQL 类型分派编辑器；未知类型回退到默认文本编辑。

    注意：int / float 使用 QLineEdit + 正则校验器，而不是 QSpinBox /
    QDoubleSpinBox —— Qt 的 SpinBox 底层是 32 位 int，处理 BIGINT
    (0 ~ 9.2e18) 时会截断，导致数据损坏。
    """

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self._model = model

    def _kind(self, index) -> str:
        cols = self._model.columns()
        c = index.column()
        if not (0 <= c < len(cols)):
            return "text"
        ctx = getattr(self._model, "_context", None)
        types = getattr(ctx, "column_types", None) or {}
        return classify(types.get(cols[c]))

    def createEditor(self, parent, option, index):
        kind = self._kind(index)

        # ---------- 普通文本：去掉原生边框，改由 QSS 提供 padding ----------
        # 修复“编辑时文字显示不全”：
        # 原生 QLineEdit 在只有 26~32px 高的单元格里，border 会挤掉
        # 一部分内容区，导致文字上下被截断。这里显式使用 setFrame(False)
        # 关闭边框，再用 stylesheet 提供一点水平内边距。
        if kind == "text":
            edit = QLineEdit(parent)
            edit.setFrame(False)
            edit.setStyleSheet("QLineEdit { padding: 0px 6px; }")
            return edit

        if kind == "bool":
            box = QComboBox(parent)
            box.addItem("NULL", None)
            box.addItem("TRUE", True)
            box.addItem("FALSE", False)
            return box
        if kind == "int":
            edit = QLineEdit(parent)
            edit.setValidator(QRegularExpressionValidator(_INT_TEXT_RE, edit))
            edit.setFrame(False)
            edit.setStyleSheet("QLineEdit { padding: 0px 6px; }")
            edit.setPlaceholderText("NULL / 整数")
            return edit
        if kind == "float":
            edit = QLineEdit(parent)
            edit.setValidator(QRegularExpressionValidator(_FLOAT_TEXT_RE, edit))
            edit.setFrame(False)
            edit.setStyleSheet("QLineEdit { padding: 0px 6px; }")
            edit.setPlaceholderText("NULL / 数值")
            return edit
        if kind == "date":
            edit = QDateEdit(parent)
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setFrame(False)
            return edit
        if kind == "time":
            edit = QTimeEdit(parent)
            edit.setDisplayFormat("HH:mm:ss")
            edit.setFrame(False)
            return edit
        if kind == "datetime":
            edit = QDateTimeEdit(parent)
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setFrame(False)
            return edit
        return super().createEditor(parent, option, index)

    def updateEditorGeometry(self, editor, option, index) -> None:
        """让编辑器在单元格内上下各留一点缓冲，避免高 DPI 下文字被截断。

        默认实现直接把 editor 撑满 option.rect，配合行高偏小时
        （例如 26px）会让下沿光标紧贴单元格边框，看起来就是
        “文字只显示了一半”。这里保留 _EDITOR_V_MARGIN 像素的上下间距。
        """
        rect = option.rect
        editor.setGeometry(
            rect.adjusted(0, _EDITOR_V_MARGIN, 0, -_EDITOR_V_MARGIN)
        )

    def setEditorData(self, editor, index):
        value = index.data(Qt.EditRole)

        if isinstance(editor, QComboBox):
            for i in range(editor.count()):
                if editor.itemData(i) == value:
                    editor.setCurrentIndex(i)
                    return
            editor.setCurrentIndex(0)
            return

        # int / float / text 使用 QLineEdit
        if isinstance(editor, QLineEdit) and not isinstance(
            editor, (QDateEdit, QTimeEdit, QDateTimeEdit)
        ):
            kind = self._kind(index)
            if kind in ("int", "float"):
                editor.setText("" if value in (None, "", "NULL") else str(value))
                return
            # 文本类型：直接走 QLineEdit 默认行为（value 是 EditRole 的 str）

        if isinstance(editor, QDateEdit):
            d = _parse_date(value)
            if d is not None:
                editor.setDate(QDate(d.year, d.month, d.day))
            return

        if isinstance(editor, QTimeEdit):
            t = _parse_time(value)
            if t is not None:
                editor.setTime(QTime(t.hour, t.minute, t.second))
            return

        if isinstance(editor, QDateTimeEdit):
            dt = _parse_datetime(value)
            if dt is not None:
                editor.setDateTime(QDateTime(
                    QDate(dt.year, dt.month, dt.day),
                    QTime(dt.hour, dt.minute, dt.second),
                ))
            return

        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentData(), Qt.EditRole)
            return

        # int / float：QLineEdit + 校验器
        if isinstance(editor, QLineEdit) and not isinstance(
            editor, (QDateEdit, QTimeEdit, QDateTimeEdit)
        ):
            kind = self._kind(index)
            if kind in ("int", "float"):
                text = editor.text().strip()
                if text == "" or text.upper() == "NULL":
                    model.setData(index, None, Qt.EditRole)
                    return
                try:
                    if kind == "int":
                        new_val = int(text)
                    else:
                        new_val = float(text)
                except ValueError:
                    return
                old = index.data(Qt.EditRole)
                try:
                    if old not in (None, "", "NULL") and float(old) == float(new_val):
                        return
                except (TypeError, ValueError):
                    pass
                model.setData(index, new_val, Qt.EditRole)
                return

        if isinstance(editor, QDateEdit):
            model.setData(index, editor.date().toPython(), Qt.EditRole)
            return
        if isinstance(editor, QTimeEdit):
            model.setData(index, editor.time().toPython(), Qt.EditRole)
            return
        if isinstance(editor, QDateTimeEdit):
            model.setData(index, editor.dateTime().toPython(), Qt.EditRole)
            return

        super().setModelData(editor, model, index)