"""Tkinter desktop front end for saved targets, workflows, and runs."""

import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .config import Config
from .errors import UserError
from .ssh import discover_aliases, discover_keys, key_paths

tk = None
ttk = None
filedialog = None
messagebox = None
simpledialog = None


def tkinter_message():
    if sys.platform == "darwin":
        version = f"python-tk@{sys.version_info.major}.{sys.version_info.minor}"
        return (f"This Python has no Tkinter, so the window cannot open. With Homebrew Python run "
                f"'brew install {version}'; otherwise install Python from python.org. Then reopen the UI.")
    if sys.platform.startswith("linux"):
        return ("This Python has no Tkinter, so the window cannot open. Install Tk support, for example "
                "'sudo apt install python3-tk' or 'sudo dnf install python3-tkinter'. Then reopen the UI.")
    return ("This Python has no Tkinter, so the window cannot open. Reinstall Python from python.org with "
            "the 'tcl/tk and IDLE' option. Then reopen the UI.")


def cli_arguments(config, arguments):
    command = [sys.executable, "-m", "transfer_docker_image"]
    if config is not None and config.path:
        command += ["--config", str(config.path)]
    return [*command, *arguments]


def target_arguments(fields, action="add"):
    arguments = ["target", action, str(fields.get("name", "")).strip(), "--non-interactive"]
    for key, flag in (("ssh_host", "--host"), ("ssh_user", "--user"), ("ssh_port", "--port"),
                      ("identity_file", "--identity"), ("host_os", "--host-os"),
                      ("remote_shell", "--shell"), ("platform", "--platform")):
        value = str(fields.get(key, "")).strip()
        if value:
            arguments += [flag, value]
    return arguments


def workflow_arguments(fields, action="add"):
    arguments = ["workflow", action, str(fields.get("name", "")).strip(), "--non-interactive"]
    for key, flag in (("source", "--source"), ("git_ref", "--git-ref"), ("target", "--target"),
                      ("build_type", "--build-type"), ("dockerfile", "--dockerfile"),
                      ("context", "--context"), ("image", "--image"),
                      ("compose_file", "--compose-file"), ("builder", "--builder")):
        value = str(fields.get(key, "")).strip()
        if value:
            arguments += [flag, value]
    services = str(fields.get("services", "")).split()
    if services:
        arguments += ["--services", *services]
    for line in str(fields.get("build_args", "")).splitlines():
        line = line.strip()
        if line:
            arguments += ["--build-arg", line]
    return arguments


def key_install_arguments(fields):
    arguments = ["key", "install", "--host", str(fields.get("ssh_host", "")).strip(),
                 "--key", str(fields.get("identity_file", "")).strip()]
    for key, flag in (("ssh_user", "--user"), ("ssh_port", "--port"), ("remote_shell", "--shell")):
        value = str(fields.get(key, "")).strip()
        if value:
            arguments += [flag, value]
    return arguments


def load_tkinter():
    global tk, ttk, filedialog, messagebox, simpledialog
    try:
        import tkinter
        from tkinter import filedialog as tk_filedialog
        from tkinter import messagebox as tk_messagebox
        from tkinter import simpledialog as tk_simpledialog
        from tkinter import ttk as tk_ttk
    except ImportError as exc:
        raise UserError(f"{tkinter_message()} ({exc})") from None
    tk, ttk = tkinter, tk_ttk
    filedialog, messagebox, simpledialog = tk_filedialog, tk_messagebox, tk_simpledialog
    return tkinter


def start(config_path=None):
    tkinter = load_tkinter()
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        raise UserError(f"Cannot open a window: {exc}. A graphical session is required.") from None
    App(root, Config(config_path))
    root.mainloop()


class App:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.process = None
        self.queue = queue.Queue()
        self.pending = None
        self.target_fields = {}
        self.target_inputs = {}
        self.workflow_fields = {}
        self.workflow_inputs = {}
        self.run_fields = {}
        self.root.title("transfer-docker-image")
        self.root.geometry("1000x760")
        self.root.minsize(900, 640)
        ttk.Style().configure("Hint.TLabel", foreground="#666666")
        self._build()
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._poll)

    def _build(self):
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self._build_targets()
        self._build_workflows()
        self._build_run()
        self._build_output(outer)

    def _field(self, parent, row, label, values=None, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        variable = tk.StringVar()
        if values is not None:
            widget = ttk.Combobox(parent, textvariable=variable, values=values)
        else:
            widget = ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky="ew", pady=2)
        if browse is not None:
            ttk.Button(parent, text="Browse…", command=browse).grid(row=row, column=2, padx=(4, 0))
        return variable, widget

    def _build_targets(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Targets")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.target_list = ttk.Treeview(left, columns=("host", "platform"), height=14)
        self.target_list.heading("#0", text="Name")
        self.target_list.heading("host", text="Host")
        self.target_list.heading("platform", text="Platform")
        self.target_list.column("#0", width=120)
        self.target_list.column("host", width=150)
        self.target_list.column("platform", width=110)
        self.target_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.target_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.target_list.configure(yscrollcommand=scrollbar.set)
        self.target_list.bind("<<TreeviewSelect>>", self._load_target)
        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Test", command=self.test_target).pack(side="left")
        ttk.Button(buttons, text="Remove", command=self.remove_target).pack(side="left", padx=4)

        form = ttk.LabelFrame(tab, text="Target settings", padding=8)
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)
        self.target_fields["name"], _ = self._field(form, 0, "Name")
        self.target_fields["ssh_host"], self.target_inputs["ssh_host"] = self._field(
            form, 1, "SSH alias or hostname", values=())
        self.target_fields["ssh_user"], _ = self._field(form, 2, "SSH username (blank uses SSH config)")
        self.target_fields["ssh_port"], _ = self._field(form, 3, "SSH port")
        self.target_fields["identity_file"], self.target_inputs["identity_file"] = self._field(
            form, 4, "Private key", values=(), browse=self._browse_identity)
        self.target_fields["host_os"], _ = self._field(form, 5, "Host OS", values=("", "windows", "macos", "linux"))
        self.target_fields["remote_shell"], _ = self._field(
            form, 6, "Remote shell", values=("", "posix", "powershell", "cmd"))
        self.target_fields["platform"], _ = self._field(form, 7, "Platform (blank detects)")
        actions = ttk.Frame(form)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Save & Test", command=self.save_target).pack(side="left")
        ttk.Button(actions, text="Generate key…", command=self.generate_key).pack(side="left", padx=4)
        ttk.Button(actions, text="Install key…", command=self.install_key).pack(side="left")
        ttk.Button(actions, text="Add to agent", command=self.add_to_agent).pack(side="left", padx=4)
        ttk.Label(form, text="A generated key gives passwordless login. The server password is asked once in the "
                             "terminal that started this app, and only the key path is saved.",
                  style="Hint.TLabel", wraplength=400, justify="left").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _browse_identity(self):
        path = filedialog.askopenfilename(title="Select a private SSH key", initialdir=str(Path.home() / ".ssh"))
        if path:
            self.target_fields["identity_file"].set(path)

    def _build_workflows(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Workflows")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.workflow_list = ttk.Treeview(left, columns=("source", "target", "type"), height=14)
        self.workflow_list.heading("#0", text="Name")
        self.workflow_list.heading("source", text="Source")
        self.workflow_list.heading("target", text="Target")
        self.workflow_list.heading("type", text="Type")
        self.workflow_list.column("#0", width=110)
        self.workflow_list.column("source", width=150)
        self.workflow_list.column("target", width=90)
        self.workflow_list.column("type", width=90)
        self.workflow_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.workflow_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.workflow_list.configure(yscrollcommand=scrollbar.set)
        self.workflow_list.bind("<<TreeviewSelect>>", self._load_workflow)
        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Remove", command=self.remove_workflow).pack(side="left")

        form = ttk.LabelFrame(tab, text="Workflow settings", padding=8)
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)
        self.workflow_fields["name"], _ = self._field(form, 0, "Name")
        self.workflow_fields["source"], _ = self._field(form, 1, "Local directory or Git URL",
                                                        browse=self._browse_source)
        self.workflow_fields["git_ref"], _ = self._field(form, 2, "Git ref (blank uses default branch)")
        self.workflow_fields["target"], self.workflow_inputs["target"] = self._field(form, 3, "Target", values=())
        self.workflow_fields["build_type"], _ = self._field(
            form, 4, "Build type", values=("", "dockerfile", "compose"))
        self.workflow_fields["dockerfile"], _ = self._field(form, 5, "Dockerfile")
        self.workflow_fields["context"], _ = self._field(form, 6, "Build context")
        self.workflow_fields["image"], _ = self._field(form, 7, "Image name:tag")
        self.workflow_fields["compose_file"], _ = self._field(form, 8, "Compose file")
        self.workflow_fields["services"], _ = self._field(form, 9, "Compose services (blank builds all)")
        self.workflow_fields["builder"], _ = self._field(form, 10, "Buildx builder")
        ttk.Label(form, text="Build args (KEY=VALUE per line)").grid(row=11, column=0, sticky="nw", pady=2)
        self.workflow_build_args = tk.Text(form, height=3, width=30)
        self.workflow_build_args.grid(row=11, column=1, sticky="ew", pady=2)
        actions = ttk.Frame(form)
        actions.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Save", command=self.save_workflow).pack(side="left")
        ttk.Label(form, text="Local sources resolve from the current directory; use Browse to capture an absolute path.",
                  style="Hint.TLabel", wraplength=400, justify="left").grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _browse_source(self):
        path = filedialog.askdirectory(title="Select the project directory")
        if path:
            self.workflow_fields["source"].set(path)

    def _build_run(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Run")
        tab.columnconfigure(1, weight=1)
        self.run_fields["name"] = tk.StringVar()
        self.run_fields["builder"] = tk.StringVar()
        ttk.Label(tab, text="Workflow").grid(row=0, column=0, sticky="w", pady=2)
        self.run_workflow_box = ttk.Combobox(tab, textvariable=self.run_fields["name"])
        self.run_workflow_box.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(tab, text="Builder override").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(tab, textvariable=self.run_fields["builder"]).grid(row=1, column=1, sticky="ew", pady=2)
        buttons = ttk.Frame(tab)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Run", command=self.start_run).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="left", padx=4)
        ttk.Button(buttons, text="Clear output", command=self.clear_output).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(tab, mode="indeterminate")
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(tab, text="Build and transfer output appears in the Output pane below. SSH prompts appear in "
                            "the terminal that started this app.",
                  style="Hint.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_output(self, outer):
        frame = ttk.LabelFrame(outer, text="Output", padding=4)
        frame.pack(fill="both", expand=False, pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        self.output = tk.Text(frame, height=13, wrap="word", state="disabled")
        self.output.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.output.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)

    def refresh(self):
        try:
            self.config = Config(self.config.path)
        except UserError as exc:
            self.append_log(f"{exc}\n")
        targets = sorted(self.config.data["targets"])
        self.target_list.delete(*self.target_list.get_children())
        for name in targets:
            value = self.config.data["targets"][name]
            self.target_list.insert("", "end", iid=name, text=name,
                                    values=(value.get("ssh_host", ""), value.get("platform", "")))
        self.target_inputs["ssh_host"].configure(values=discover_aliases())
        self.target_inputs["identity_file"].configure(values=[str(path) for path in discover_keys()])
        workflows = sorted(self.config.data["workflows"])
        self.workflow_list.delete(*self.workflow_list.get_children())
        for name in workflows:
            value = self.config.data["workflows"][name]
            self.workflow_list.insert("", "end", iid=name, text=name,
                                      values=(value.get("source", ""), value.get("target", ""),
                                              value.get("build_type", "")))
        self.workflow_inputs["target"].configure(values=targets)
        self.run_workflow_box.configure(values=workflows)

    def _load_target(self, _event=None):
        selection = self.target_list.selection()
        if not selection:
            return
        value = self.config.data["targets"].get(selection[0], {})
        self.target_fields["name"].set(selection[0])
        for key in ("ssh_host", "ssh_user", "ssh_port", "identity_file", "host_os", "remote_shell", "platform"):
            self.target_fields[key].set(str(value.get(key, "")))

    def _load_workflow(self, _event=None):
        selection = self.workflow_list.selection()
        if not selection:
            return
        value = self.config.data["workflows"].get(selection[0], {})
        self.workflow_fields["name"].set(selection[0])
        for key in ("source", "git_ref", "target", "build_type", "dockerfile", "context", "image",
                    "compose_file", "builder"):
            self.workflow_fields[key].set(str(value.get(key, "")))
        self.workflow_fields["services"].set(" ".join(value.get("services", [])))
        self.workflow_build_args.delete("1.0", "end")
        self.workflow_build_args.insert("1.0", "\n".join(f"{key}={item}" for key, item in value.get("build_args", {}).items()))

    @staticmethod
    def _values(variables):
        return {key: variable.get().strip() for key, variable in variables.items()}

    @staticmethod
    def _selected(tree):
        selection = tree.selection()
        return selection[0] if selection else ""

    def save_target(self):
        fields = self._values(self.target_fields)
        if not fields["name"]:
            messagebox.showwarning("Target", "Enter a target name.")
            return
        if not fields["ssh_host"]:
            messagebox.showwarning("Target", "Enter an SSH alias or hostname.")
            return
        action = "edit" if fields["name"] in self.config.data["targets"] else "add"
        self.run_cli(target_arguments(fields, action))

    def test_target(self):
        name = self._selected(self.target_list)
        if not name:
            messagebox.showwarning("Target", "Select a saved target first.")
            return
        self.run_cli(["target", "test", name])

    def remove_target(self):
        name = self._selected(self.target_list)
        if not name:
            messagebox.showwarning("Target", "Select a saved target first.")
            return
        if messagebox.askokcancel("Remove target", f"Remove target {name}?"):
            self.run_cli(["target", "remove", name])

    def generate_key(self):
        current = self.target_fields["identity_file"].get().strip()
        default = current if current and not current.endswith(".pub") else str(Path.home() / ".ssh" / "id_ed25519")
        path = simpledialog.askstring("Generate SSH key", "Private key path", initialvalue=default, parent=self.root)
        if not path or not path.strip():
            return
        path = path.strip()

        def finished():
            private, public = key_paths(path)
            self.target_fields["identity_file"].set(str(private))
            try:
                text = public.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.append_log(f"Public key {public}:\n{text}\n")
                messagebox.showinfo("SSH key", f"Created {private}.\nThe public key was copied to your clipboard.")

        self.run_cli(["key", "generate", path], on_success=finished)

    def install_key(self):
        fields = self._values(self.target_fields)
        if not fields["ssh_host"]:
            messagebox.showwarning("SSH key", "Enter an SSH alias or hostname first.")
            return
        if not fields["identity_file"]:
            messagebox.showwarning("SSH key", "Generate or choose a private key first.")
            return
        self.append_log("If the server asks for its password or host-key confirmation, answer in the terminal "
                        "that started this app.\n")
        self.run_cli(key_install_arguments(fields),
                     on_success=lambda: self.append_log("Public key installed. Save & Test to store the key on the target.\n"))

    def add_to_agent(self):
        path = self.target_fields["identity_file"].get().strip()
        if not path:
            messagebox.showwarning("SSH key", "Choose a private key first.")
            return
        self.run_command(["ssh-add", str(Path(path).expanduser())])

    def save_workflow(self):
        fields = self._values(self.workflow_fields)
        fields["build_args"] = self.workflow_build_args.get("1.0", "end")
        if not fields["name"]:
            messagebox.showwarning("Workflow", "Enter a workflow name.")
            return
        action = "edit" if fields["name"] in self.config.data["workflows"] else "add"
        self.run_cli(workflow_arguments(fields, action))

    def remove_workflow(self):
        name = self._selected(self.workflow_list)
        if not name:
            messagebox.showwarning("Workflow", "Select a saved workflow first.")
            return
        if messagebox.askokcancel("Remove workflow", f"Remove workflow {name}?"):
            self.run_cli(["workflow", "remove", name])

    def start_run(self):
        name = self.run_fields["name"].get().strip()
        if not name:
            messagebox.showwarning("Run", "Choose a workflow.")
            return
        arguments = ["run", name]
        builder = self.run_fields["builder"].get().strip()
        if builder:
            arguments += ["--builder", builder]
        self.run_cli(arguments)

    def run_cli(self, arguments, on_success=None):
        self.run_command(cli_arguments(self.config, arguments), on_success)

    def run_command(self, command, on_success=None):
        if self.process is not None:
            messagebox.showinfo("Busy", "A command is still running. Wait for it or click Cancel.")
            return
        command = [str(part) for part in command]
        self.append_log(f"\n$ {shlex.join(command)}\n")
        self.pending = on_success
        options = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace", bufsize=1,
                       env=dict(os.environ, BUILDKIT_PROGRESS="plain"))
        if os.name == "posix":
            options["start_new_session"] = True
        else:
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self.process = subprocess.Popen(command, **options)
        except OSError as exc:
            self.process = None
            self.pending = None
            messagebox.showerror("Cannot run", str(exc))
            return
        self.progress.start(80)
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()

    def _pump(self, process):
        for line in process.stdout:
            self.queue.put(("line", line))
        self.queue.put(("exit", process.wait()))

    def _poll(self):
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "line":
                    self.append_log(value)
                else:
                    self._finish(value)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self, code):
        self.process = None
        self.progress.stop()
        callback, self.pending = self.pending, None
        if code == 0:
            self.append_log("Done.\n")
            if callback is not None:
                callback()
        else:
            self.append_log(f"Failed with exit code {code}.\n")
        self.refresh()

    def append_log(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def clear_output(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def cancel(self):
        process = self.process
        if process is None:
            self.append_log("Nothing is running.\n")
            return
        self.append_log("Stopping…\n")
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except OSError as exc:
            self.append_log(f"Could not stop the process: {exc}\n")

    def _close(self):
        if self.process is not None and not messagebox.askokcancel("Quit", "A command is running. Stop it and quit?"):
            return
        if self.process is not None:
            self.cancel()
        self.root.destroy()
