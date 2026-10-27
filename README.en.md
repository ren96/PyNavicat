# PyNavicat
🌐 [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/PySide6-6.5%2B-green)

> A lightweight, cross-platform database client built with **Python + PySide6 + SQLAlchemy**.
> Designed for everyday table browsing, SQL debugging, data import/export, and schema comparison.

Supports **15 database engines**, and ships with a SQL editor, ER diagrams, execution-plan
visualization, schema diff, an import/export wizard, and a command palette
(`Ctrl+P` / `Ctrl+Shift+P`).

---

## Table of Contents

- [Features](#features)
- [Supported Databases](#supported-databases)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Configuration](#configuration)
- [Security Notes](#security-notes)
- [Project Layout](#project-layout)
- [Building a Standalone Executable](#building-a-standalone-executable)
- [Known Limitations](#known-limitations)
- [FAQ](#faq)
- [Development](#development)
- [License](#license)

---

## Features

### Object Browser
- Multi-level lazy loading: connection → database → table / view / procedure / function / trigger / event → column
- Connection status dots (online / offline)
- Connection grouping, color tags, and read-only markers
- Search box with optional **regular expression** filtering (toggle via the `.*` button)
- Global search: find **table names and column names** by keyword across all databases in the current connection
- Context menu: create database, create table, generate DDL, alter structure, manage indexes, drop, import data…

### SQL Editor
- Syntax highlighting for keywords, strings, numbers, comments, and identifiers
- Line-number gutter, current-line highlight, **current-statement highlight**
- Context-aware autocompletion:
  - Typing `table.` pops up that table's columns
  - `FROM` / `JOIN` pops up table names
  - Global completion for database names, table names, column names, and keywords
- One-click formatting (`Ctrl+Shift+F`; only formats the selection when there is one)
- Find / Replace (`Ctrl+F` / `Ctrl+H`)
- Auto-close for brackets and quotes, smart indentation
- Line operations: duplicate, delete, move up/down, toggle comment

### Result Grid
- Multiple result sub-tabs (one per statement in a multi-statement batch)
- Server-side pagination (`LIMIT / OFFSET`, with automatic dialect handling for SQL Server and Oracle)
- Column sorting and global filtering
- **Editable** (requires a primary key and a non-read-only connection):
  - Cell editing, insert row, delete row
  - Undo / Redo (`Ctrl+Z` / `Ctrl+Y`)
  - Batched `UPDATE / INSERT / DELETE` statements generated on save
  - Edited cells are highlighted
- **Typed editors** dispatched by the column's SQL type:
  `int` / `float` / `bool` / `date` / `time` / `datetime` / `text`.
  BIGINT keeps full precision (validated `QLineEdit` instead of `QSpinBox`).
- **Sensitive-column masking**: columns like `password`, `token`, `secret`, `credit_card` are masked automatically and can be unmasked with one click
- **Value → SQL shortcuts**: right-click to copy as literal / WHERE clause / INSERT statement / SELECT column list
- Large-field viewer: double-click to view full content; binary values are auto-detected and shown as UTF-8 or hex
- Clipboard paste: Tab-separated multi-row, multi-column blocks paste directly into the grid

### Import / Export
- **Export**: CSV / JSON / Excel / INSERT statements
  - Full result / filtered result / selected rows
  - Column selection, row limit, encoding (UTF-8 / UTF-8-SIG / GBK / GB18030)
  - Batch size and conflict strategy (`insert` / `ignore` / `replace` / `upsert`)
- **Import**:
  - CSV / JSON / Excel (`.xlsx` / `.xlsm`)
  - Column-mapping wizard: file column ↔ table column, with the option to ignore a column
  - Conflict strategy, batch size, and "skip existing rows" (matched by primary or unique key)
  - **If the target table does not exist, column types are inferred and a `CREATE TABLE` preview is shown** — editable before execution
  - Data preview of the first 20 rows

### Execution-Plan Visualization
- Auto-detects `EXPLAIN` output and switches to a tree view
- **MySQL**: `type=ALL` / `index`, unused candidate index, `filesort`, `temporary`, million-row scan, etc.
- **PostgreSQL**: `Seq Scan`, `Nested Loop`, sort spilling to disk, and more
- **SQLite**: full scan, temporary B-Tree
- Color-coded summary chips (critical / warning / good), plus a condensed list of hints
- One-click toggle to view the raw output

### Schema Diff
- Compare tables and columns between two databases (or two connections)
- Generates migration SQL (`DROP` / `CREATE` / `ALTER`); direction is source **A → target B**, and the script runs **on A**
- Supports MySQL `MODIFY COLUMN` and PostgreSQL `ALTER COLUMN … TYPE / SET / DROP`
- Detail table plus a "copy all SQL" button

### ER Diagram
- One draggable node per table, showing the table name and the first 18 columns
- Foreign keys drawn as curved edges
- `Ctrl + scroll` to zoom; rubber-band selection
- Filter by table name
- Double-click a node to open that table's data

### Session / Variables Panel
- **MySQL**: session variables / global variables / global status / process list
- **PostgreSQL**: `pg_settings` / `pg_stat_activity`
- **SQLite**: `PRAGMA database_list`
- Searchable, right-click to copy

### Transactions
- Manual-transaction mode: once enabled, statements run inside a transaction and must be explicitly **committed** or **rolled back**
- Prompts on tab close or disconnect if a transaction is still open
- Read-only connections cannot start a write transaction

### Query Cancellation
- Cancel a running query with `Esc` or the **Stop** button
- Prefers driver-level `cancel()`; falls back to force-closing the underlying socket if the driver stalls

### Command Palette
- `Ctrl+P`: search and jump to tables / views / open tabs / connections / databases
- `Ctrl+Shift+P`: search and execute application commands (new, refresh, theme, settings, import, disconnect…)
- Fuzzy matching (substring + subsequence scoring), with shortcut hints

### Snippets / SQL History
- Saved snippets: name, tags, SQL, associated connection / database
- Double-click a snippet to insert it into the current editor
- SQL history: success / failure marker; double-click to open in a new tab; delete or clear

### Other
- Light / dark themes; the editor follows the active theme
- Multi-window support (`Ctrl+Shift+N`) for side-by-side comparison
- Tab restoration and undo-close (`Ctrl+Shift+T`)
- Confirmation before dangerous operations: `DROP`, `TRUNCATE`, `COPY FROM`, `DELETE` / `UPDATE` without `WHERE`, `SELECT … INTO`
- **Read-only connection**: a per-connection switch that blocks all writes, DDL, and grid editing
- **SSH tunnel**: password / private key / ssh-agent / Pageant
- All icons are drawn with `QPainter` (no external assets), so they render identically across platforms

---

## Supported Databases

| Type | SQLAlchemy Driver | Default Port | Notes |
|------|-------------------|--------------|-------|
| MySQL | `mysql+pymysql` | 3306 | 5.5+ / 8.0 |
| MariaDB | `mysql+pymysql` | 3306 | Reuses the MySQL driver |
| TiDB | `mysql+pymysql` | 4000 | MySQL protocol compatible |
| OceanBase | `mysql+pymysql` | 2881 | MySQL mode |
| PostgreSQL | `postgresql+psycopg2` | 5432 | 9.6+ |
| CockroachDB | `postgresql+psycopg2` | 26257 | PostgreSQL protocol compatible |
| KingBase | `postgresql+psycopg2` | 54321 | PostgreSQL protocol compatible |
| SQL Server | `mssql+pyodbc` | 1433 | Requires a system-level ODBC driver |
| Oracle | `oracle+oracledb` | 1521 | Thin mode, no Instant Client needed |
| IBM DB2 | `ibm_db_sa+db2` | 50000 | Requires a system-level DB2 client |
| Firebird | `firebird+fdb` | 3050 | — |
| SQLite | `sqlite` | — | File-based, built into Python |
| DuckDB | `duckdb` | — | File-based |
| ClickHouse | `clickhouse+http` | 8123 | HTTP interface (native is 9000) |
| Trino | `trino` | 8080 | Distributed query engine |

---

## Requirements

- **Python**: 3.10 or newer (3.11 / 3.12 recommended)
- **OS**: Windows 10+, macOS 12+, or a mainstream Linux distribution
- **Disk**: ~500 MB including PySide6

> On Windows, SQL Server requires **Microsoft ODBC Driver 18 for SQL Server**.
> Oracle uses thin mode and needs no extra components. DB2 requires a system-level client.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/<your-name>/pynavicat.git
cd pynavicat
```

### 2. Create a virtual environment

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (CMD)**

```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Install additional drivers as needed:

```bash
pip install pyodbc oracledb duckdb-engine trino[sqlalchemy] clickhouse-sqlalchemy
```

### 4. Prepare icons (optional)

`config.py` looks for an icon directory in this order and uses the first hit:

1. Environment variable `DBCLIENT_ICON_DIR`
2. `dbclient/resources/`
3. `<project-root>/tubiao/`
4. `dbclient/tubiao/`

Place `MySQL客户端图标设计.ico` and `MySQL客户端图标设计.png` in any of these.
If none are found, the app silently falls back to no icon — nothing breaks.

### 5. Launch

```bash
python main.py
```

---

## Quick Start

1. **Create a connection**: toolbar → **+ New Connection** → pick a database type → fill in the details → Save
2. **Browse objects**: expand a connection → database → table / view / …, and click a table to open its data
3. **Write SQL**: press `Ctrl+T` to open a new query tab, type SQL, then run it with `Ctrl+Enter` or `Ctrl+R`
4. **Export data**: click **Export ▾** at the top-right of the result grid, then choose format and scope
5. **Import data**: press `Ctrl+I` or click **📥 Import Data** in the toolbar, pick a file → pick a target → map columns → confirm

---

## Keyboard Shortcuts

### Global

| Shortcut | Action |
|----------|--------|
| `Ctrl+T` | New query tab |
| `Ctrl+R` | Execute the current SQL |
| `Ctrl+I` | Import data from a local file |
| `F5` | Refresh the object tree |
| `Ctrl+P` | Go to object (command palette in object mode) |
| `Ctrl+Shift+P` | Command palette |
| `Ctrl+Shift+T` | Reopen the last closed tab |
| `Ctrl+Shift+E` | Show the ER diagram |
| `Ctrl+Shift+M` | Schema diff |
| `Ctrl+Shift+N` | New window |
| `Ctrl+,` | Settings |

### Inside a Query Tab

| Shortcut | Action |
|----------|--------|
| `Ctrl+Enter` / `Ctrl+R` | Execute SQL |
| `Esc` | Cancel the running query |
| `Ctrl+Shift+F` | Format (only the selection when there is one) |
| `Ctrl+F` / `Ctrl+H` | Find / Replace |
| `Ctrl+D` | Duplicate the current line or selection |
| `Ctrl+Shift+D` | Delete the current line |
| `Ctrl+/` | Toggle line comment |
| `Alt+↑` / `Alt+↓` | Move the current line up / down |

### Inside the Result Grid

| Shortcut | Action |
|----------|--------|
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+S` | Save changes |
| `Ctrl+V` | Paste (multi-row, multi-column, Tab-separated) |

---

## Configuration

### Data Directory

On first launch, the app picks the first **writable** data directory from:

1. The path given in the `DBCLIENT_HOME` environment variable
2. `shujv/` next to the project root (source run) or next to the executable (frozen run)
3. `pynavicat/` inside the system temp directory

This directory contains:

| File | Purpose |
|------|---------|
| `connections.json` | Connection list (the password field holds ciphertext, or is empty) |
| `secrets.key` / `secrets.salt` | Random key and salt for local encryption (PBKDF2 inputs) |
| `sql_history.json` | SQL execution history |
| `sql_snippets.json` | Saved query snippets |
| `pycat.log` | Runtime log (rotating, max 2 MB × 4) |

> **Important**: if `secrets.key` / `secrets.salt` are lost, locally encrypted passwords
> cannot be recovered. Back them up together with `connections.json`,
> and **never commit them to Git**.

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `DBCLIENT_HOME` | Overrides the data directory |
| `DBCLIENT_ICON_DIR` | Overrides the icon directory |

### Application Settings

Menu **File → Settings…** or `Ctrl+,`:

- Theme (light / dark)
- Editor font size
- Default page size
- Per-query row limit
- Confirm-dangerous-operations toggle
- Sensitive-column masking toggle

---

## Security Notes

### Password Storage

A two-tier strategy, preferring the OS credential store:

1. **OS keychain** (recommended — Windows Credential Manager, macOS Keychain, Linux SecretService)
2. **Local encryption** (fallback when the keychain is unavailable):
   - Algorithm: `Fernet (AES-128-CBC + HMAC-SHA256)`
   - Key derivation: `PBKDF2-HMAC-SHA256` with 200,000 iterations
   - Derivation input: machine fingerprint (user + hostname + OS + arch + MAC) + a 32-byte random key stored in `secrets.key`
   - Salt: the 16-byte random salt stored in `secrets.salt`
   - On-disk format: `v2:<fernet_token>` — plaintext is never written
   - If `cryptography` is completely unavailable, falls back to base64 (only used to read legacy data)

### Read-Only Connections

A per-connection switch. When enabled, the connection:

- Rejects `INSERT` / `UPDATE` / `DELETE` / DDL / privilege / maintenance statements
- Makes the result grid non-editable
- Cannot start a write transaction

Recommended when connecting to production.

### Dangerous-Operation Confirmation

The following operations show an SQL preview dialog before execution:

- `DROP TABLE / DATABASE / SCHEMA / VIEW / INDEX / USER / ROLE`
- `TRUNCATE`
- `COPY … FROM` (PostgreSQL import)
- `GRANT` / `REVOKE`
- `DELETE` / `UPDATE` **without a `WHERE` clause**
- `SELECT … INTO OUTFILE / DUMPFILE`

Can be disabled in **Settings → Query**.

### Before Publishing or Sharing

If you push this project to GitHub or share it with someone:

```bash
# Verify the data directory is not tracked
git ls-files | grep -E 'shujv|connections\.json|secrets\.(key|salt)'

# If anything shows up, untrack it:
git rm -r --cached shujv/
git commit -m "chore: stop tracking local data dir"
```

**Rotate any exposed credentials immediately** — if both the ciphertext and the
local encryption key leak, that is equivalent to a plaintext leak.

---

## Project Layout

```
pynavicat/
├── main.py                     # Entry point
├── requirements.txt
├── README.md
├── .gitignore
├── tubiao/                     # Icon assets
│   ├── MySQL客户端图标设计.ico
│   └── MySQL客户端图标设计.png
└── dbclient/
    ├── __init__.py
    ├── config.py               # Paths / QSettings / app metadata
    ├── theme.py                # Palette and QSS
    ├── crypto.py               # Local Fernet encryption
    ├── connection.py           # Connection config, persistence, password management
    ├── connection_type_dialog.py
    ├── engine.py               # DatabaseClient: metadata / execution / cancel / transactions
    ├── sql_util.py             # Identifier quoting / SQL literals
    ├── sql_history.py
    ├── sql_snippets.py
    ├── ssh_tunnel.py
    ├── io_tools.py             # Import/export, type inference, CREATE TABLE generation
    ├── schema_ddl.py           # Reverse-engineered CREATE TABLE
    ├── logging_setup.py
    ├── main_window.py
    ├── dialogs.py              # Connection / create DB / create table / ALTER / index / import / create-table preview
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

## Building a Standalone Executable

Using PyInstaller (Windows example):

```bat
pip install pyinstaller
pyinstaller ^
    --name PyNavicat ^
    --windowed ^
    --icon "tubiao/MySQL客户端图标设计.ico" ^
    --add-data "tubiao;tubiao" ^
    main.py
```

On Linux / macOS, replace `^` with `\` and `;` with `:`:

```bash
pyinstaller \
    --name PyNavicat \
    --windowed \
    --icon "tubiao/MySQL客户端图标设计.ico" \
    --add-data "tubiao:tubiao" \
    main.py
```

The app detects `sys.frozen` and:

- Loads icons from `_MEIPASS/tubiao/`
- Places the data directory at `shujv/` next to the executable

**Size optimization** — trim unused modules with:

```
--exclude-module tkinter --exclude-module matplotlib --exclude-module numpy
```

**External dependencies** — a standalone executable does not require Python on the
target machine, but the following still need to be installed by the user when relevant:

- SQL Server → Microsoft ODBC Driver for SQL Server
- DB2 → a system-level DB2 client
- Oracle (thick mode, i.e. 11g and older) → Oracle Instant Client

---

## Known Limitations

- **Grid editing requires a primary key**: tables without one are read-only to avoid accidental mass updates
- **Oracle create/drop database**: requires `DBCA`; not supported by the client
- **SQLite / DuckDB**: no `CREATE DATABASE`; use separate files per database
- **Metadata reflection is globally serialized**: all reflection goes through a single
  process-wide lock to avoid PyMySQL SSL races. Large databases take longer on first expand.
- **Execution-plan parsing is heuristic**: based on text patterns, not an official AST.
  Unknown formats are shown as-is.
- **Schema diff direction is fixed**: the script always runs on **A** (source) and turns A into B.

---

## FAQ

**Q: There are no connections after launch.**
First run opens **New Connection** automatically. If it was dismissed, click
**+ New Connection** in the toolbar.

**Q: I have to re-enter my password every time.**
This means both the OS keychain and local encryption failed. Check that
`shujv/secrets.key` and `shujv/secrets.salt` exist and are writable.

**Q: Why is the result grid read-only?**
One of the following is true:
1. The current query is not `SELECT * FROM <table>` (aggregates, joins, subqueries, etc.), so a row cannot be located unambiguously
2. The table has no primary key
3. **Read-only connection** is enabled

**Q: Imported data is mojibake.**
In the import wizard, change **Encoding** to `GBK` or `GB18030`
(Excel-saved CSVs default to GBK on Chinese Windows).

**Q: SQL Server reports "driver not found".**
`pip install pyodbc` only installs the Python binding. You also need to install
[Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).

**Q: Oracle reports `DPI-1047` or Instant Client errors.**
This project uses `oracledb` in **thin mode**, which does not need Oracle Instant Client.
If you see Instant Client errors, an old `cx_Oracle` may still be installed —
run `pip uninstall cx_Oracle` and reinstall `oracledb`.

**Q: Startup takes a few seconds.**
On first launch the app probes the OS keychain (2-second timeout) and asynchronously
pre-warms tabs and connections from the previous session. You can disable session
restore in **Settings** if you do not need it.

---

## Development

### Linting / Formatting

```bash
pip install ruff
ruff check .
ruff format --check .
```

### Runtime Logs

- File: `<data-dir>/pycat.log` (rotating, max 2 MB × 4)
- Console: `WARNING` and above only
- Menu **Help → Open Log Directory** opens it directly

### Isolated Data Directory for Debugging

```bash
# Linux / macOS
DBCLIENT_HOME=/tmp/pynavicat-dev python main.py

# Windows PowerShell
$env:DBCLIENT_HOME="C:\tmp\pynavicat-dev"; python main.py
```

---

## License

Released under the MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [PySide6](https://doc.qt.io/qtforpython/) — Qt for Python
- [SQLAlchemy](https://www.sqlalchemy.org/) — Python SQL toolkit
- [sqlparse](https://github.com/andialbrecht/sqlparse) — SQL formatter
- [cryptography](https://cryptography.io/) — Fernet / PBKDF2
- [keyring](https://github.com/jaraco/keyring) — OS credential store
- [openpyxl](https://openpyxl.readthedocs.io/) — Excel I/O
- [sshtunnel](https://github.com/pahaz/sshtunnel) — SSH tunnels