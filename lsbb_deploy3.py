import configparser
import os
import pathlib
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from typing import Any
from tkinter import messagebox, scrolledtext, ttk


REPO_ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = REPO_ROOT / "script_setup.yaml"
DB_CONFIG_PATH = REPO_ROOT / "db_config.ini"
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEPLOYMENTS = {
    "FULL DEPLOYMENT": [["lscriptnxp2.py"], ["eth_deploy.py"], ["sx_deploy3.py"]],
    "NXP DEPLOYMENT": [["lscriptnxp2.py"]],
    "SWITCH DEPLOYMENT": [["eth_deploy.py"]],
    "SX DEPLOYMENT": [["sx_deploy3.py"]],
    "Test PCBA": [["test_jig3.py"]],
    "Test SYSTEM": [["test_sys3.py"]],
}

DEPLOYMENT_STAGE = {
    "NXP DEPLOYMENT": "nxp",
    "SWITCH DEPLOYMENT": "switch",
    "SX DEPLOYMENT": "sx",
}

SCRIPT_STAGE = {
    "lscriptnxp2.py": "nxp",
    "eth_deploy.py": "switch",
    "sx_deploy3.py": "sx",
}

TEST_DEPLOYMENTS = {"Test PCBA", "Test SYSTEM"}
STAGE_ORDER = ("nxp", "switch", "sx")
SUCCESS_STATUS = "success"
FAILED_STATUS = "failed"
RUNNING_STATUS = "running"
PENDING_STATUS = "pending"
MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{12}$|^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
DIG_SN_PATTERN = re.compile(r"^(?:CLS|MLS)DM-\d{2}-\d{4}-(?:\d{6}|[A-Z]\d)-\d{3,5}$")


@dataclass(frozen=True)
class SqlConfig:
    server: str
    database: str
    username: str
    password: str
    schema: str
    progress_table: str
    mac_table: str
    driver_candidates: tuple[str, ...]
    connection_string: str
    timeout_seconds: int

    @property
    def progress_object_name(self) -> str:
        return f"{self.schema}.{self.progress_table}"

    @property
    def mac_object_name(self) -> str:
        return f"{self.schema}.{self.mac_table}"

    @property
    def progress_qualified_name(self) -> str:
        return f"[{self.schema}].[{self.progress_table}]"

    @property
    def mac_qualified_name(self) -> str:
        return f"[{self.schema}].[{self.mac_table}]"


def validate_sql_identifier(name: str, label: str) -> str:
    if not SQL_IDENTIFIER_PATTERN.fullmatch(name):
        raise RuntimeError(f"Invalid SQL identifier for {label}: {name}")
    return name


def load_sql_config(path: pathlib.Path = DB_CONFIG_PATH) -> SqlConfig:
    if not path.is_file():
        raise RuntimeError(f"Missing SQL config file: {path}")
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if "sql_server" not in parser:
        raise RuntimeError(f"Missing [sql_server] section in {path}")
    section = parser["sql_server"]
    server = os.environ.get("SERVER_NAME", section.get("server", "")).strip()
    database = os.environ.get("DB_NAME", section.get("database", "")).strip()
    username = os.environ.get("DB_LOGIN", section.get("username", "")).strip()
    password = os.environ.get("DB_PASSWORD", section.get("password", "")).strip()
    schema = validate_sql_identifier(section.get("schema", "dbo").strip(), "schema")
    progress_table = validate_sql_identifier(section.get("progress_table", "chip_deployment_progress").strip(), "progress_table")
    mac_table = validate_sql_identifier(section.get("mac_table", "chip_deployment_macs").strip(), "mac_table")
    drivers = tuple(
        driver.strip()
        for driver in section.get("driver_candidates", "").split(",")
        if driver.strip()
    )
    if not drivers:
        drivers = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server Native Client 11.0", "SQL Server")
    connection_string = section.get(
        "connection_string",
        "DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password};Encrypt=no;TrustServerCertificate=yes;",
    ).strip()
    timeout_seconds = section.getint("timeout_seconds", fallback=5)
    missing = [name for name, value in {"server": server, "database": database, "username": username, "password": password}.items() if not value]
    if missing:
        raise RuntimeError(f"Missing SQL config values in {path}: {', '.join(missing)}")
    return SqlConfig(
        server=server,
        database=database,
        username=username,
        password=password,
        schema=schema,
        progress_table=progress_table,
        mac_table=mac_table,
        driver_candidates=drivers,
        connection_string=connection_string,
        timeout_seconds=timeout_seconds,
    )


def load_simple_yaml(path: pathlib.Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        key, _, remainder = stripped.partition(":")
        key = key.strip()
        remainder = remainder.strip()
        if not key:
            continue
        if remainder == "":
            nested: dict[str, Any] = {}
            stack[-1][1][key] = nested
            stack.append((indent, nested))
        else:
            stack[-1][1][key] = remainder.strip("\"'")
    return root


def normalize_mac(mac: str) -> str:
    text = mac.strip()
    if not MAC_PATTERN.fullmatch(text):
        raise ValueError(f"Invalid MAC address: {mac}")
    compact = text.replace(":", "").replace("-", "").upper()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def normalize_serial(serial: str) -> str:
    normalized = serial.strip().upper()
    if not DIG_SN_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Unsupported serial format.\n\n"
            "Expected examples:\n"
            "CLSDM-09-0926-260528-002\n"
            "MLSDM-08-0726-B1-00010"
        )
    return normalized


def get_pyodbc_module():
    try:
        import pyodbc  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("pyodbc is required for SQL Server access. Install pyodbc and a SQL Server ODBC driver.") from exc
    return pyodbc


def open_sql_connection():
    config = load_sql_config()
    pyodbc = get_pyodbc_module()
    installed = set(pyodbc.drivers())
    drivers = [driver for driver in config.driver_candidates if driver in installed]
    if not drivers:
        drivers = list(config.driver_candidates)
    last_error: Exception | None = None
    for driver in drivers:
        connection_string = config.connection_string.format(
            driver=driver,
            server=config.server,
            database=config.database,
            username=config.username,
            password=config.password,
        )
        try:
            return pyodbc.connect(connection_string, timeout=config.timeout_seconds)
        except pyodbc.Error as exc:
            last_error = exc
    raise RuntimeError(f"Could not connect to SQL Server {config.server}/{config.database}: {last_error}")


def ensure_sql_tables(connection) -> None:
    config = load_sql_config()
    cursor = connection.cursor()
    stage_columns = ", ".join(f"{stage}_status NVARCHAR(20) NOT NULL DEFAULT '{PENDING_STATUS}'" for stage in STAGE_ORDER)
    cursor.execute(
        f"""
IF OBJECT_ID(N'{config.progress_object_name}', N'U') IS NULL
BEGIN
    CREATE TABLE {config.progress_qualified_name} (
        serial_number NVARCHAR(100) NOT NULL PRIMARY KEY,
        current_stage NVARCHAR(20) NOT NULL DEFAULT 'nxp',
        {stage_columns},
        last_error NVARCHAR(MAX) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    )
END
"""
    )
    mac_columns = ", ".join(f"mac{index} NVARCHAR(17) NULL" for index in range(1, 17))
    cursor.execute(
        f"""
IF OBJECT_ID(N'{config.mac_object_name}', N'U') IS NULL
BEGIN
    CREATE TABLE {config.mac_qualified_name} (
        serial_number NVARCHAR(100) NOT NULL PRIMARY KEY,
        {mac_columns},
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    )
END
"""
    )
    connection.commit()


def read_deployment_row(serial: str) -> dict[str, str] | None:
    config = load_sql_config()
    with open_sql_connection() as connection:
        ensure_sql_tables(connection)
        cursor = connection.cursor()
        cursor.execute(
            f"SELECT current_stage, nxp_status, switch_status, sx_status FROM {config.progress_qualified_name} WHERE serial_number = ?",
            serial,
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "current_stage": row.current_stage,
        "nxp_status": row.nxp_status,
        "switch_status": row.switch_status,
        "sx_status": row.sx_status,
    }


def ensure_deployment_row(connection, serial: str) -> None:
    config = load_sql_config()
    connection.cursor().execute(
        f"""
IF NOT EXISTS (SELECT 1 FROM {config.progress_qualified_name} WHERE serial_number = ?)
BEGIN
    INSERT INTO {config.progress_qualified_name} (serial_number, current_stage, updated_at)
    VALUES (?, 'nxp', SYSUTCDATETIME())
END
""",
        serial,
        serial,
    )


def set_stage_status(serial: str, stage: str, status: str, error: str | None = None) -> None:
    config = load_sql_config()
    next_stage = stage
    if status == SUCCESS_STATUS:
        stage_index = STAGE_ORDER.index(stage)
        next_stage = STAGE_ORDER[min(stage_index + 1, len(STAGE_ORDER) - 1)]
    with open_sql_connection() as connection:
        ensure_sql_tables(connection)
        ensure_deployment_row(connection, serial)
        connection.cursor().execute(
            f"""
UPDATE {config.progress_qualified_name}
SET {stage}_status = ?,
    current_stage = ?,
    last_error = ?,
    updated_at = SYSUTCDATETIME()
WHERE serial_number = ?
""",
            status,
            next_stage,
            error,
            serial,
        )
        connection.commit()


def read_sqlite_macs_for_serial(serial: str) -> list[str]:
    config = load_simple_yaml(CONFIG_PATH)
    db_config = config["db"]
    db_path = pathlib.Path(str(db_config["path"]))
    table = str(db_config.get("table", "mac_table"))
    serial_column = str(db_config.get("serial_column", "serial_number"))
    mac_format = str(db_config.get("mac_column_format", "mac{index}"))
    mac_count = int(db_config.get("mac_count", 16))
    mac_columns = [mac_format.format(index=index) for index in range(1, mac_count + 1)]
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT {', '.join(mac_columns)} FROM {table} WHERE {serial_column} = ?",
            (serial,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"No MAC row found in SQLite DB for serial {serial}.")
    macs = [normalize_mac(str(row[column])) for column in mac_columns]
    if len(macs) != 16:
        raise RuntimeError(f"Expected 16 MAC addresses for serial {serial}, got {len(macs)}.")
    return macs


def save_macs_to_sql(serial: str, macs: list[str]) -> None:
    config = load_sql_config()
    columns = [f"mac{index}" for index in range(1, 17)]
    column_list = ", ".join(columns)
    source_values = ", ".join("?" for _ in columns)
    update_values = ", ".join(f"{column} = source.{column}" for column in columns)
    insert_columns = "serial_number, " + column_list
    insert_values = "source.serial_number, " + ", ".join(f"source.{column}" for column in columns)
    with open_sql_connection() as connection:
        ensure_sql_tables(connection)
        connection.cursor().execute(
            f"""
MERGE {config.mac_qualified_name} AS target
USING (SELECT ? AS serial_number, {source_values}) AS source ({insert_columns})
ON target.serial_number = source.serial_number
WHEN MATCHED THEN
    UPDATE SET {update_values}, updated_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT ({insert_columns})
    VALUES ({insert_values});
""",
            serial,
            *macs,
        )
        connection.commit()


def allowed_deployments_for_row(row: dict[str, str] | None) -> set[str]:
    allowed = {"FULL DEPLOYMENT", "NXP DEPLOYMENT"}
    if row is None:
        return allowed
    if row.get("nxp_status") == SUCCESS_STATUS:
        allowed.discard("NXP DEPLOYMENT")
        allowed.add("SWITCH DEPLOYMENT")
    if row.get("switch_status") == SUCCESS_STATUS:
        allowed.discard("SWITCH DEPLOYMENT")
        allowed.add("SX DEPLOYMENT")
    if row.get("sx_status") == SUCCESS_STATUS:
        allowed.discard("SX DEPLOYMENT")
    return allowed


class DeployGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LSBB Deployment")
        self.geometry("760x520")
        self.minsize(620, 420)

        self.output_queue: queue.Queue[str] = queue.Queue()
        self.current_process: subprocess.Popen[str] | None = None
        self.buttons: dict[str, ttk.Button] = {}
        self.last_result: tuple[str, bool] | None = None

        self._build_ui()
        self.serial_var.trace_add("write", self._on_serial_changed)
        self._refresh_buttons_from_db()
        self.after(100, self._drain_output_queue)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top_frame = ttk.Frame(self, padding=(16, 16, 16, 8))
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(1, weight=1)

        ttk.Label(top_frame, text="Serial").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.serial_var = tk.StringVar()
        serial_entry = ttk.Entry(top_frame, textvariable=self.serial_var, font=("Segoe UI", 11))
        serial_entry.grid(row=0, column=1, sticky="ew")
        serial_entry.focus_set()
        self.save_sfp_var = tk.BooleanVar(value=False)
        self.save_sfp_checkbox = ttk.Checkbutton(
            top_frame,
            text="Save SFP",
            variable=self.save_sfp_var,
            command=self._refresh_buttons_from_db,
        )
        self.save_sfp_checkbox.grid(row=1, column=1, sticky="w", pady=(8, 0))

        button_frame = ttk.Frame(self, padding=(16, 8))
        button_frame.grid(row=1, column=0, sticky="ew")
        for index in range(3):
            button_frame.columnconfigure(index, weight=1)

        for index, label in enumerate(DEPLOYMENTS):
            button = ttk.Button(
                button_frame,
                text=label,
                command=lambda name=label: self.start_deployment(name),
            )
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4, ipady=8)
            self.buttons[label] = button

        output_frame = ttk.Frame(self, padding=(16, 8))
        output_frame.grid(row=2, column=0, sticky="nsew")
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)

        self.output_text = scrolledtext.ScrolledText(
            output_frame,
            wrap=tk.WORD,
            state="disabled",
            font=("Consolas", 10),
        )
        self.output_text.grid(row=0, column=0, sticky="nsew")

        bottom_frame = ttk.Frame(self, padding=(16, 8, 16, 16))
        bottom_frame.grid(row=3, column=0, sticky="ew")
        bottom_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bottom_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        exit_button = ttk.Button(bottom_frame, text="Exit", command=self.on_exit)
        exit_button.grid(row=0, column=1, sticky="e", padx=(10, 0), ipadx=12)

        self.protocol("WM_DELETE_WINDOW", self.on_exit)

    def start_deployment(self, deployment_name: str) -> None:
        if self.current_process is not None and self.current_process.poll() is None:
            messagebox.showwarning("Deployment running", "Wait for the current deployment to finish.")
            return

        serial = self.serial_var.get().strip()
        if not serial:
            messagebox.showwarning("Missing serial", "Enter a serial number before starting deployment.")
            return
        try:
            serial = normalize_serial(serial)
            self.serial_var.set(serial)
        except ValueError as exc:
            messagebox.showerror("Invalid serial", str(exc))
            return

        commands = [
            [sys.executable, *script_args, "--dig_sn", serial]
            for script_args in DEPLOYMENTS[deployment_name]
        ]
        if deployment_name == "Test SYSTEM" and self.save_sfp_var.get():
            for command in commands:
                if pathlib.Path(command[1]).name == "test_sys3.py":
                    command.append("--save-sfp")

        self._clear_output()
        for command in commands:
            self._append_output(f"> {' '.join(command)}\n")
        self._append_output("\n")
        self.status_var.set(f"Running {deployment_name}...")
        self._set_buttons_enabled(False)

        thread = threading.Thread(target=self._run_commands, args=(commands, deployment_name, serial), daemon=True)
        thread.start()

    def _run_commands(self, commands: list[list[str]], deployment_name: str, serial: str) -> None:
        success = False
        failure_text = ""
        try:
            for command in commands:
                script_name = pathlib.Path(command[1]).name
                stage = SCRIPT_STAGE.get(script_name)
                if stage is not None:
                    set_stage_status(serial, stage, RUNNING_STATUS)
                self.output_queue.put(f"\nStarting {' '.join(command[1:])}\n")
                #self.current_process = subprocess.Popen(
                #    command,
                #    cwd=REPO_ROOT,
                #    stdout=subprocess.PIPE,
                #    stderr=subprocess.STDOUT,
                #    stdin=subprocess.DEVNULL,
                #    text=True,
                #    bufsize=1,
                #    encoding="utf-8",
                #    errors="replace",
                #)

                self.current_process = subprocess.Popen(command, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,)

                #assert self.current_process.stdout is not None
                #for line in self.current_process.stdout:
                #    self.output_queue.put(line)

                

                return_code = self.current_process.wait()
                if return_code != 0:
                    failure_text = f"{' '.join(command[1:])} failed with exit code {return_code}."
                    if stage is not None:
                        set_stage_status(serial, stage, FAILED_STATUS, failure_text)
                    self.output_queue.put(f"\n{deployment_name} stopped: {failure_text}\n")
                    return
                if stage is not None:
                    set_stage_status(serial, stage, SUCCESS_STATUS)
                    if stage == "nxp":
                        macs = read_sqlite_macs_for_serial(serial)
                        save_macs_to_sql(serial, macs)
                        self.output_queue.put(f"\nSaved 16 NXP MAC addresses to SQL Server for {serial}.\n")

            success = True
            self.output_queue.put(f"\n{deployment_name} finished successfully.\n")
        except Exception as exc:
            failure_text = str(exc)
            if commands:
                script_name = pathlib.Path(commands[0][1]).name
                stage = DEPLOYMENT_STAGE.get(deployment_name) or SCRIPT_STAGE.get(script_name)
                if stage is not None:
                    try:
                        set_stage_status(serial, stage, FAILED_STATUS, failure_text)
                    except Exception:
                        pass
            self.output_queue.put(f"\nFailed to run {deployment_name}: {exc}\n")
        finally:
            self.last_result = (deployment_name, success)
            self.output_queue.put("__DEPLOYMENT_DONE__")

    def _drain_output_queue(self) -> None:
        try:
            while True:
                message = self.output_queue.get_nowait()
                if message == "__DEPLOYMENT_DONE__":
                    self.current_process = None
                    self.status_var.set("Ready")
                    self._finish_deployment_ui()
                else:
                    self._append_output(message)
        except queue.Empty:
            pass

        self.after(100, self._drain_output_queue)

    def _append_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)
        self.output_text.configure(state="disabled")

    def _clear_output(self) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.configure(state="disabled")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons.values():
            button.configure(state=state)
        self.save_sfp_checkbox.configure(state=state)

    def _on_serial_changed(self, *_args: object) -> None:
        if self.current_process is not None and self.current_process.poll() is None:
            return
        self._refresh_buttons_from_db()

    def _refresh_buttons_from_db(self) -> None:
        serial = self.serial_var.get().strip()
        allowed = {"FULL DEPLOYMENT", "NXP DEPLOYMENT", *TEST_DEPLOYMENTS}
        if serial:
            try:
                allowed = allowed_deployments_for_row(read_deployment_row(serial)) | TEST_DEPLOYMENTS
                self.status_var.set("Ready")
            except Exception as exc:
                allowed = set(TEST_DEPLOYMENTS)
                self.status_var.set(f"SQL DB error: {exc}")
        for label, button in self.buttons.items():
            button.configure(state="normal" if label in allowed else "disabled")
        if self.save_sfp_var.get():
            for label in ("FULL DEPLOYMENT", "NXP DEPLOYMENT", "Test PCBA"):
                if label in self.buttons:
                    self.buttons[label].configure(state="disabled")

    def _finish_deployment_ui(self) -> None:
        deployment_name, success = self.last_result or ("Deployment", False)
        if deployment_name == "FULL DEPLOYMENT":
            messagebox.showinfo(
                "Full Deployment",
                "Full deployment passed." if success else "Full deployment failed.",
            )
            self.serial_var.set("")
            self._clear_output()
        self._refresh_buttons_from_db()

    def on_exit(self) -> None:
        if self.current_process is not None and self.current_process.poll() is None:
            if not messagebox.askyesno(
                "Deployment running",
                "A deployment is still running. Exit and stop it?",
            ):
                return
            self.current_process.terminate()
        self.destroy()


def main() -> None:
    app = DeployGui()
    app.mainloop()


if __name__ == "__main__":
    main()
