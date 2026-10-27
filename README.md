# PyNavicat

🌐 [简体中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/PySide6-6.5%2B-green)

> 一个用 **Python + PySide6 + SQLAlchemy** 编写的轻量级跨平台数据库客户端。
> 面向日常库表浏览、SQL 调试、数据导入导出与结构对比。

支持 **15 种数据库**，内置 SQL 编辑器、ER 图、执行计划可视化、表结构对比、
导入导出向导、命令面板（Ctrl+P / Ctrl+Shift+P）等特性。

---

## 目录

- [功能特性](#功能特性)
- [支持的数据库](#支持的数据库)
- [系统要求](#系统要求)
- [安装](#安装)
- [快速上手](#快速上手)
- [快捷键](#快捷键)
- [配置](#配置)
- [安全说明](#安全说明)
- [项目结构](#项目结构)
- [打包为可执行文件](#打包为可执行文件)
- [已知限制](#已知限制)
- [常见问题](#常见问题)
- [开发](#开发)
- [许可](#许可)

---

## 功能特性

### 对象浏览器
- 多级懒加载：连接 → 数据库 → 表 / 视图 / 存储过程 / 函数 / 触发器 / 事件 → 列
- 连接状态圆点（在线 / 离线）
- 连接分组、彩色标记、只读标记
- 顶部搜索框，支持 **正则表达式** 过滤（`.*` 按钮切换）
- 全局搜索：在当前连接的所有库中按关键字搜索 **表名 / 列名**
- 右键菜单：新建库、新建表、生成 DDL、修改结构、管理索引、删除、导入数据…

### SQL 编辑器
- 关键字 / 字符串 / 数字 / 注释 / 标识符语法高亮
- 行号栏、当前行高亮、**当前语句块高亮**
- 上下文感知自动补全：
  - 输入 `表名.` 弹出该表的列
  - `FROM` / `JOIN` 后弹出表名
  - 全局补全库名、表名、列名、关键字
- 一键格式化（`Ctrl+Shift+F`，可只格式化选中片段）
- 查找 / 替换（`Ctrl+F` / `Ctrl+H`）
- 括号 / 引号自动闭合，智能缩进
- 行操作：复制行、删除行、上下移动行、切换注释

### 结果表格
- 多结果子页签（多语句分别展示）
- 分页浏览（服务端 `LIMIT / OFFSET`，自动识别 SQL Server / Oracle）
- 列排序、全局过滤
- **可编辑**（依赖主键与连接是否只读）：
  - 单元格修改、插入行、删除行
  - 撤销 / 重做（`Ctrl+Z` / `Ctrl+Y`）
  - 统一生成 `UPDATE / INSERT / DELETE` 保存
  - 修改单元格高亮
- **类型化编辑器**：按列的 SQL 类型自动分派
  `int` / `float` / `bool` / `date` / `time` / `datetime` / `text`，
  BIGINT 不丢精度（用带正则校验的 `QLineEdit`，不用 SpinBox）
- **敏感列脱敏**：`password` / `token` / `secret` / `credit_card` 等列名自动打码，可一键关闭
- **值 → SQL 快捷通道**：右键复制为字面量 / WHERE 条件 / INSERT 语句 / SELECT 列名
- 大字段查看器（双击查看全文，二进制自动识别 UTF-8 / Hex）
- 剪贴板粘贴（Tab 分隔多行多列直接写入）

### 导入 / 导出
- **导出**：CSV / JSON / Excel / INSERT 语句
  - 全量 / 过滤结果 / 选中行
  - 列勾选、行数上限、编码（UTF-8 / UTF-8-SIG / GBK / GB18030）
  - 批量大小、冲突策略（`insert` / `ignore` / `replace` / `upsert`）
- **导入**：
  - 支持 CSV / JSON / Excel（`.xlsx` / `.xlsm`）
  - 列映射向导：文件列 ↔ 表列，可忽略列
  - 冲突策略、批量大小、跳过已存在（按主键 / 唯一键判重）
  - **目标表不存在时自动推断列类型并生成 `CREATE TABLE` 预览**，可编辑后执行
  - 数据预览（前 20 行）

### 执行计划可视化
- 自动识别 `EXPLAIN` 输出并切换为树形视图
- **MySQL**：`type=ALL` / `index` / 有候选索引未使用 / filesort / temporary / 百万行扫描等告警
- **PostgreSQL**：`Seq Scan` / `Nested Loop` / 排序溢出到磁盘等
- **SQLite**：全表扫描 / 临时 B-Tree
- 彩色 chip 汇总：严重 / 警告 / 良好数量，附精简提示
- 一键切换「查看原始数据」

### 表结构对比
- 两个库（或两个连接）的 **表和列差异**
- 生成迁移 SQL（`DROP` / `CREATE` / `ALTER`），方向：源 A → 目标 B，脚本在 **A** 上执行
- 支持 MySQL 的 `MODIFY COLUMN`、PostgreSQL 的 `ALTER COLUMN … TYPE / SET / DROP`
- 差异详情表格 + 一键复制全部 SQL

### ER 图
- 每张表一个可拖动节点，显示表名 + 前 18 列
- 外键用曲线连接
- `Ctrl + 滚轮` 缩放，橡皮筋框选
- 过滤（按表名筛选）
- 双击节点打开该表的数据

### 会话 / 变量面板
- **MySQL**：会话变量 / 全局变量 / 全局状态 / 进程列表
- **PostgreSQL**：`pg_settings` / `pg_stat_activity`
- **SQLite**：`PRAGMA database_list`
- 可搜索过滤，右键复制

### 事务
- 手动事务模式：开启后 SQL 在事务中执行，需显式「提交」或「回滚」
- 页签关闭 / 连接断开时提示未提交事务
- 只读连接禁止开启写事务

### 查询取消
- `Esc` 或「停止」按钮取消正在执行的查询
- 驱动层 `cancel()` 优先；卡死时强制关闭底层 socket 兜底

### 命令面板
- `Ctrl+P`：搜索并跳转到表 / 视图 / 已打开页签 / 连接 / 数据库
- `Ctrl+Shift+P`：搜索并执行应用命令（新建、刷新、主题、设置、导入、断开连接…）
- 模糊匹配（子串 + 子序列评分），带快捷键提示

### 查询片段 / SQL 历史
- 已保存片段：名称、标签、SQL、关联连接 / 库
- 双击片段插入当前编辑器
- SQL 历史：成功 / 失败标记，双击在新页签打开，可删除 / 清空

### 其他
- 浅色 / 深色主题，编辑器同步换肤
- 多窗口（`Ctrl+Shift+N`），可并排对比
- 页签恢复与撤销关闭（`Ctrl+Shift+T`）
- 危险操作确认：`DROP` / `TRUNCATE` / `COPY FROM` / 无 `WHERE` 的 `DELETE` / `UPDATE` / `SELECT INTO`
- **只读连接**：连接级开关，禁止所有写操作、DDL、结果表编辑
- **SSH 隧道**：密码 / 私钥 / ssh-agent / Pageant 三种认证方式
- 图标全为矢量绘制（`QPainter`），不依赖外部资源，跨平台一致

---

## 支持的数据库

| 类型 | SQLAlchemy 驱动 | 默认端口 | 备注 |
|------|----------------|---------|------|
| MySQL | `mysql+pymysql` | 3306 | 5.5+ / 8.0 |
| MariaDB | `mysql+pymysql` | 3306 | 复用 MySQL 驱动 |
| TiDB | `mysql+pymysql` | 4000 | MySQL 协议兼容 |
| OceanBase | `mysql+pymysql` | 2881 | MySQL 模式 |
| PostgreSQL | `postgresql+psycopg2` | 5432 | 9.6+ |
| CockroachDB | `postgresql+psycopg2` | 26257 | PG 协议兼容 |
| KingBase（人大金仓） | `postgresql+psycopg2` | 54321 | PG 协议兼容 |
| SQL Server | `mssql+pyodbc` | 1433 | 需系统级 ODBC 驱动 |
| Oracle | `oracle+oracledb` | 1521 | thin 模式，无需 Instant Client |
| IBM DB2 | `ibm_db_sa+db2` | 50000 | 需系统级 DB2 客户端 |
| Firebird | `firebird+fdb` | 3050 | — |
| SQLite | `sqlite` | — | 文件型，内置 |
| DuckDB | `duckdb` | — | 文件型 |
| ClickHouse | `clickhouse+http` | 8123 | HTTP 接口（native 为 9000） |
| Trino | `trino` | 8080 | 分布式查询引擎 |

---

## 系统要求

- **Python**：3.10 及以上（推荐 3.11 / 3.12）
- **操作系统**：Windows 10+ / macOS 12+ / 主流 Linux 发行版
- **磁盘**：约 500 MB（含 PySide6）

> Windows 上如需 SQL Server，请另装 **Microsoft ODBC Driver 18 for SQL Server**；
> Oracle 使用 thin 模式无需额外组件；DB2 需系统级客户端。

---

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/<your-name>/pynavicat.git
cd pynavicat
```

### 2. 创建虚拟环境

**Windows（PowerShell）**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows（CMD）**

```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

按需追加数据库驱动（示例）：

```bash
pip install pyodbc oracledb duckdb-engine trino[sqlalchemy] clickhouse-sqlalchemy
```

### 4. 准备图标（可选）

`config.py` 会按以下顺序查找图标目录，命中第一个即可：

1. 环境变量 `DBCLIENT_ICON_DIR`
2. `dbclient/resources/`
3. `项目根/tubiao/`
4. `dbclient/tubiao/`

把 `MySQL客户端图标设计.ico` 和 `MySQL客户端图标设计.png` 放进任意一个命中目录。
找不到时会静默降级为无图标（不影响功能）。

### 5. 启动

```bash
python main.py
```

---

## 快速上手

1. **新建连接**：工具栏「＋ 新建连接」→ 选择数据库类型 → 填写参数 → 保存
2. **浏览对象**：左侧展开连接 → 数据库 → 表 / 视图 / … ，单击表自动打开数据
3. **写 SQL**：`Ctrl+T` 新建查询页签，输入 SQL，`Ctrl+Enter` 或 `Ctrl+R` 执行
4. **导出数据**：结果表右上角「导出 ▾」，选择格式与范围
5. **导入数据**：`Ctrl+I` 或工具栏「📥 导入数据」，选文件 → 选目标 → 列映射 → 确认

---

## 快捷键

### 全局

| 快捷键 | 功能 |
|-------|------|
| `Ctrl+T` | 新建查询页签 |
| `Ctrl+R` | 执行当前 SQL |
| `Ctrl+I` | 从本机导入数据 |
| `F5` | 刷新对象树 |
| `Ctrl+P` | 跳转到对象（命令面板 · 对象模式） |
| `Ctrl+Shift+P` | 命令面板 |
| `Ctrl+Shift+T` | 撤销关闭页签 |
| `Ctrl+Shift+E` | 查看 ER 图 |
| `Ctrl+Shift+M` | 表结构对比 |
| `Ctrl+Shift+N` | 新建窗口 |
| `Ctrl+,` | 设置 |

### 查询页签内

| 快捷键 | 功能 |
|-------|------|
| `Ctrl+Enter` / `Ctrl+R` | 执行 SQL |
| `Esc` | 取消正在执行的查询 |
| `Ctrl+Shift+F` | 格式化（选中时只格式化选中片段） |
| `Ctrl+F` / `Ctrl+H` | 查找 / 替换 |
| `Ctrl+D` | 复制当前行或选区 |
| `Ctrl+Shift+D` | 删除当前行 |
| `Ctrl+/` | 切换行注释 |
| `Alt+↑` / `Alt+↓` | 上下移动当前行 |

### 结果表格内

| 快捷键 | 功能 |
|-------|------|
| `Ctrl+Z` / `Ctrl+Y` | 撤销 / 重做 |
| `Ctrl+S` | 保存修改 |
| `Ctrl+V` | 粘贴（支持多行多列 Tab 分隔） |

---

## 配置

### 数据目录

程序首次启动时会按以下优先级选择一个 **可写** 的数据目录：

1. 环境变量 `DBCLIENT_HOME` 指定的路径
2. 项目根目录下的 `shujv/`（源码运行）或 exe 同级的 `shujv/`（打包后）
3. 系统临时目录下的 `pynavicat/`

该目录包含：

| 文件 | 说明 |
|------|------|
| `connections.json` | 连接列表（密码字段为密文或空） |
| `secrets.key` / `secrets.salt` | 本地加密的随机密钥与盐（PBKDF2 输入） |
| `sql_history.json` | SQL 执行历史 |
| `sql_snippets.json` | 已保存的查询片段 |
| `pycat.log` | 运行日志（滚动，最大 2 MB × 4） |

> **重要**：`secrets.key` / `secrets.salt` 一旦丢失，本地加密的密码将无法解密。
> 请与 `connections.json` 一起备份；**不要提交到 Git 仓库**。

### 环境变量

| 变量 | 作用 |
|------|------|
| `DBCLIENT_HOME` | 覆盖数据目录位置 |
| `DBCLIENT_ICON_DIR` | 覆盖图标目录位置 |

### 应用设置

菜单「文件 → 设置…」或 `Ctrl+,`：

- 主题（浅色 / 深色）
- 编辑器字号
- 默认分页大小
- 单次返回上限
- 危险操作确认开关
- 敏感列脱敏开关

---

## 安全说明

### 密码存储

采用 **两级策略**，优先使用系统凭据服务：

1. **系统钥匙串**（推荐，Windows 走 Credential Manager，macOS 走 Keychain，Linux 走 SecretService）
2. **本地加密**（钥匙串不可用时）：
   - 算法：`Fernet (AES-128-CBC + HMAC-SHA256)`
   - 密钥派生：`PBKDF2-HMAC-SHA256`，20 万次迭代
   - 派生输入：机器指纹（用户名 + 主机名 + 系统 + 架构 + MAC）+ `secrets.key` 中的 32 字节随机密钥
   - 盐：`secrets.salt` 中的 16 字节随机盐
   - 落盘格式：`v2:<fernet_token>`，绝不落明文
   - `cryptography` 完全不可用时退回 base64（仅用于兼容读取旧数据）

### 只读连接

连接级开关，启用后该连接上：

- 拒绝执行 `INSERT` / `UPDATE` / `DELETE` / DDL / 权限 / 维护类语句
- 结果表不可编辑
- 无法开启写事务

适合连接生产库时使用。

### 危险操作确认

以下操作执行前会弹窗预览 SQL：

- `DROP TABLE / DATABASE / SCHEMA / VIEW / INDEX / USER / ROLE`
- `TRUNCATE`
- `COPY ... FROM`（PostgreSQL 导入）
- `GRANT` / `REVOKE`
- **不带 `WHERE` 的 `DELETE` / `UPDATE`**
- `SELECT ... INTO OUTFILE / DUMPFILE`

可在「设置 → 查询」中关闭。

### 上传 / 分享前请检查

如果你把这个项目推到 GitHub 或分享给别人：

```bash
# 确认 data dir 没被追踪
git ls-files | grep -E 'shujv|connections\.json|secrets\.(key|salt)'

# 有输出则说明误提交，执行：
git rm -r --cached shujv/
git commit -m "chore: stop tracking local data dir"
```

**已泄露的密码请立即更换**——加密密钥与密文同时泄露等于明文泄露。

---

## 项目结构

```
pynavicat/
├── main.py                     # 启动入口
├── requirements.txt
├── README.md
├── .gitignore
├── tubiao/                     # 图标资源
│   ├── MySQL客户端图标设计.ico
│   └── MySQL客户端图标设计.png
└── dbclient/
    ├── __init__.py
    ├── config.py               # 路径 / QSettings / 应用元信息
    ├── theme.py                # 主题调色板与 QSS
    ├── crypto.py               # 本地 Fernet 加密
    ├── connection.py           # 连接配置 + 持久化 + 密码管理
    ├── connection_type_dialog.py
    ├── engine.py               # DatabaseClient：元数据 / 执行 / 取消 / 事务
    ├── sql_util.py             # 标识符引用 / SQL 字面量
    ├── sql_history.py
    ├── sql_snippets.py
    ├── ssh_tunnel.py
    ├── io_tools.py             # 导入导出 / 类型推断 / 建表
    ├── schema_ddl.py           # 反向生成 CREATE TABLE
    ├── logging_setup.py
    ├── main_window.py
    ├── dialogs.py              # 连接/建库/建表/ALTER/索引/导入/建表预览
    ├── about_dialog.py
    ├── settings_dialog.py
    └── widgets/
        ├── __init__.py
        ├── command_palette.py
        ├── delegates.py
        ├── er_diagram.py
        ├── explain_view.py
        ├── export_dialog.py
        ├── history_panel.py
        ├── object_browser.py
        ├── query_tab.py
        ├── result_view.py
        ├── schema_diff_dialog.py
        ├── session_panel.py
        ├── snippets_panel.py
        └── sql_editor.py
```

---

## 打包为可执行文件

使用 PyInstaller（Windows 示例）：

```bash
pip install pyinstaller
pyinstaller ^
    --name PyNavicat ^
    --windowed ^
    --icon "tubiao/MySQL客户端图标设计.ico" ^
    --add-data "tubiao;tubiao" ^
    main.py
```

Linux / macOS 请把 `^` 换成 `\`，`;` 换成 `:`：

```bash
pyinstaller \
    --name PyNavicat \
    --windowed \
    --icon "tubiao/MySQL客户端图标设计.ico" \
    --add-data "tubiao:tubiao" \
    main.py
```

程序会自动检测 `sys.frozen` 状态，并：

- 从 `_MEIPASS/tubiao/` 加载图标
- 将数据目录放在 exe 同级 `shujv/` 下

**体积优化**：如需裁剪体积，可在 PyInstaller 命令中加入：

```
--exclude-module tkinter --exclude-module matplotlib --exclude-module numpy
```

**加密依赖**：如果只发布 exe，用户机器上无需安装 Python，但 SQL Server 的 ODBC 驱动、DB2 客户端、Oracle Instant Client（thick 模式）仍需用户自行安装。

---

## 已知限制

- **结果表编辑需要主键**：无主键表只读，避免误更新多行
- **Oracle 创建 / 删除数据库**：需通过 `DBCA`，客户端不提供
- **SQLite / DuckDB**：无 `CREATE DATABASE` 概念，通过文件路径区分库
- **元数据反射全局串行**：所有反射操作共享一把进程级锁，避免 PyMySQL SSL 竞态，但大库首次展开会较慢
- **执行计划解析**：基于文本启发式，非官方 AST；未知格式原样展示
- **表结构对比方向**：脚本始终在 **A**（源）库执行，方向为「把 A 改成 B」

---

## 常见问题

**Q：启动后没有任何连接？**
首次运行会自动弹出「新建连接」，若被取消，点击工具栏「＋ 新建连接」即可。

**Q：密码每次都要重新输入？**
说明系统钥匙串不可用且本地加密也失败。检查 `shujv/secrets.key` 与 `shujv/secrets.salt` 是否可写、是否被误删。

**Q：结果表无法编辑？**
三种可能：
1. 当前查询不是 `SELECT * FROM 表名`（例如聚合、JOIN、子查询），程序无法定位到具体行
2. 表没有主键
3. 当前连接勾选了「只读连接」

**Q：导入时中文乱码？**
在导入向导里把「编码」改为 `GBK` 或 `GB18030`（Excel 另存为 CSV 默认是 GBK）。

**Q：SQL Server 提示「未找到驱动程序」？**
`pip install pyodbc` 只装了 Python 绑定，还需另装 [Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)。

**Q：Oracle 报 `DPI-1047` / Instant Client 相关错误？**
本程序使用 `oracledb` 的 **thin 模式**，不需要 Oracle Instant Client；若遇到相关错误，说明同时存在旧版 `cx_Oracle`，请 `pip uninstall cx_Oracle` 后重装 `oracledb`。

**Q：启动卡住几秒？**
首次启动会探测系统钥匙串（超时 2 秒），并异步预热上次打开的页签与连接。若不需要恢复，可在设置里关闭。

---

## 开发

### 代码检查

```bash
pip install ruff
ruff check .
ruff format --check .
```

### 运行日志

- 文件：`<数据目录>/pycat.log`（滚动，最大 2 MB × 4）
- 控制台：仅输出 `WARNING` 及以上
- 菜单「帮助 → 打开日志目录」可直接打开

### 调试数据目录

```bash
# Linux / macOS
DBCLIENT_HOME=/tmp/pynavicat-dev python main.py

# Windows PowerShell
$env:DBCLIENT_HOME="C:\tmp\pynavicat-dev"; python main.py
```

---

## 许可

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。

---

## 致谢

- [PySide6](https://doc.qt.io/qtforpython/) — Qt for Python
- [SQLAlchemy](https://www.sqlalchemy.org/) — Python SQL 工具包
- [sqlparse](https://github.com/andialbrecht/sqlparse) — SQL 格式化
- [cryptography](https://cryptography.io/) — Fernet / PBKDF2
- [keyring](https://github.com/jaraco/keyring) — 系统凭据服务
- [openpyxl](https://openpyxl.readthedocs.io/) — Excel 读写
- [sshtunnel](https://github.com/pahaz/sshtunnel) — SSH 隧道