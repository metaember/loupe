"""Local project stages with content-bound evidence and fail-closed releases.

Only explicit manifest file lists enter releases. Commands are argv arrays, never
shell strings. No upload, printer, flashing, package installation or ordering API.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class WorkflowError(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(f.name, path)


def tool_versions():
    """Actual executables and package environment, with no dependency installation."""
    info = {
        "python": sys.version,
        "executable": str(
            Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        ),
    }
    from pcb import kicad_cli

    for name, command in [
        ("uv", [shutil.which("uv") or "uv", "--version"]),
        ("kicad", [str(kicad_cli()), "--version"]),
    ]:
        r = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise WorkflowError(f"{name} is unavailable: {r.stderr}")
        info[name] = {"path": command[0], "version": r.stdout.strip()}
    return info


class Project:
    def __init__(self, path="."):
        path = Path(path).resolve()
        self.config_path = path if path.is_file() else path / "loupe.json"
        self.root = self.config_path.parent
        self.config = json.loads(self.config_path.read_text())
        if self.config.get("version") != 1:
            raise WorkflowError("unsupported loupe.json version")
        self.stages = {s["name"]: s for s in self.config["stages"]}
        if len(self.stages) != len(self.config["stages"]):
            raise WorkflowError("duplicate stage names")
        self.state = self.root / ".loupe"
        self.state.mkdir(exist_ok=True)
        self._tools = None
        self.validate()

    def validate(self):
        seen = set()
        for name, stage in self.stages.items():
            if not name.replace("-", "").replace("_", "").isalnum():
                raise WorkflowError(f"invalid stage name: {name}")
            if stage["kind"] not in ["build", "check", "review"]:
                raise WorkflowError(f"invalid stage kind: {name}")
            if not isinstance(stage["command"], list) or not stage["command"]:
                raise WorkflowError(f"command must be argv: {name}")
            for dep in stage.get("requires", []):
                if dep not in seen:
                    raise WorkflowError(f"{name}: dependency must precede stage: {dep}")
            if not stage.get("inputs") or not stage.get("outputs"):
                raise WorkflowError(f"{name}: explicit inputs/outputs required")
            seen.add(name)
        # Generated output files are never globbed; typo/missing files must fail.
        for stage in self.stages.values():
            for value in stage["outputs"]:
                self.path(value)

    def path(self, value):
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root):
            raise WorkflowError(f"path escapes project: {value}")
        if any(p in [".venv", ".git"] for p in path.relative_to(self.root).parts):
            raise WorkflowError(f"private/build environment path: {value}")
        if path.name == "secrets.yaml" or path.name.startswith(".env"):
            raise WorkflowError(f"credential path prohibited: {value}")
        return path

    def files(self, patterns, required=True):
        found = {}
        for pattern in patterns:
            self.path(
                pattern
            )  # validate before expansion, including .. and absolute paths
            files = sorted(p for p in self.root.glob(pattern) if p.is_file())
            if not files and required:
                raise WorkflowError(f"no input matches: {pattern}")
            for p in files:
                checked = self.path(str(p.relative_to(self.root)))
                found[str(checked.relative_to(self.root))] = sha(checked)
        return found

    def context(self, stage):
        if self._tools is None:
            self._tools = tool_versions()
        return {
            "manifest": sha(self.config_path),
            "stage": stage,
            "inputs": self.files(stage["inputs"]),
            "optional_inputs": self.files(stage.get("optional_inputs", []), required=False),
            "tools": self._tools,
            "tool_sources": {p.name: sha(p) for p in sorted(ROOT.glob("*.py"))},
        }

    def record_path(self, name):
        return self.state / f"{name}.json"

    def fresh(self, name, stack=None):
        stage = self.stages[name]
        stack = (stack or []) + [name]
        try:
            record = json.loads(self.record_path(name).read_text())
            if record["context"] != self.context(stage):
                return False, "inputs, configuration or toolchain changed"
            current = self.files(stage["outputs"])
            if record["outputs"] != current:
                return False, "outputs changed"
            for dep in stage.get("requires", []):
                ok, reason = self.fresh(dep, stack)
                if not ok:
                    return False, f"{dep}: {reason}"
                if record["dependencies"].get(dep) != sha(self.record_path(dep)):
                    return False, f"{dep}: evidence changed"
            return True, "fresh"
        except (OSError, KeyError, ValueError, WorkflowError) as exc:
            return False, str(exc)

    def run_stage(self, name, force=False):
        stage = self.stages[name]
        for dep in stage.get("requires", []):
            ok, reason = self.fresh(dep)
            if not ok:
                raise WorkflowError(f"{name} requires fresh {dep}: {reason}")
        if not force and self.fresh(name)[0]:
            print(f"{name}: fresh", flush=True)
            return
        self.record_path(name).unlink(missing_ok=True)
        context = self.context(stage)
        from pcb import kicad_cli, kicad_python

        tokens = {
            "python": sys.executable,
            "project": str(self.root),
            "loupe": str(ROOT),
            "uv": shutil.which("uv") or "uv",
        }
        if any("{kicad_cli}" in v for v in stage["command"]):
            tokens["kicad_cli"] = str(kicad_cli())
        if any("{kicad_python}" in v for v in stage["command"]):
            tokens["kicad_python"] = str(kicad_python())
        command = []
        for value in stage["command"]:
            for key, replacement in tokens.items():
                value = value.replace("{" + key + "}", replacement)
            command.append(value)
        env = dict(os.environ, LOUPE_ROOT=str(ROOT))
        print(f"{name}: running", flush=True)
        # Stderr/stdout remain visible, and the complete stage log is retained locally.
        with (self.state / f"{name}.log").open("w") as log:
            if stage.get("stdout_report"):
                result = subprocess.run(
                    command, cwd=self.root, env=env, capture_output=True, text=True
                )
                log.write(result.stdout + result.stderr)
                print(result.stderr, end="", flush=True)
                self.path(stage["stdout_report"]).write_text(result.stdout)
                code = result.returncode
            else:
                process = subprocess.Popen(
                    command,
                    cwd=self.root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in process.stdout:
                    log.write(line)
                    print(line, end="", flush=True)
                process.stdout.close()
                code = process.wait()
        if code:
            raise WorkflowError(f"{name} failed ({code}); see .loupe/{name}.log")
        # Build scripts may update their outputs, never undeclared checked source inputs.
        if self.context(stage) != context:
            raise WorkflowError(
                f"{name}: inputs changed during execution; evidence not recorded"
            )
        outputs = self.files(stage["outputs"])
        for file in stage.get("reports", []):
            if json.loads(self.path(file).read_text()).get("ok") is not True:
                raise WorkflowError(f"{name}: report did not pass: {file}")
        dependencies = {
            dep: sha(self.record_path(dep)) for dep in stage.get("requires", [])
        }
        atomic_json(
            self.record_path(name),
            dict(context=context, outputs=outputs, dependencies=dependencies),
        )
        print(f"{name}: passed", flush=True)

    def run(self, kind, force=False):
        selected = [
            name for name, stage in self.stages.items() if stage["kind"] == kind
        ]
        if not selected:
            raise WorkflowError(f"no {kind} stages declared")
        for name in selected:
            self.run_stage(name, force)

    def status(self):
        result = {}
        for name in self.stages:
            ok, reason = self.fresh(name)
            result[name] = {"status": "passed" if ok else "stale", "reason": reason}
        return result

    def release(self, target="jlcpcb"):
        if target not in self.config["release"]["targets"]:
            raise WorkflowError(f"unsupported release target: {target}")
        for name in self.stages:
            ok, reason = self.fresh(name)
            if not ok:
                raise WorkflowError(f"release blocked by {name}: {reason}")
        release = self.config["release"]
        outputs, pending = [], []
        delivery = self.path(release.get("directory", "delivery"))
        delivery.mkdir(parents=True, exist_ok=True)
        manifest_path = self.path(release["manifest"])
        if not manifest_path.is_relative_to(delivery):
            raise WorkflowError("release manifest must be inside delivery directory")
        covered = set()
        for stage in self.stages.values():
            covered.update(self.files(stage["inputs"]))
            covered.update(self.files(stage["outputs"]))
        with tempfile.TemporaryDirectory(
            prefix="loupe-release-", dir=self.state
        ) as work:
            for index, bundle in enumerate(release["bundles"]):
                payload = {}
                for name in bundle["files"]:
                    if any(c in name for c in "*?["):
                        raise WorkflowError("release files must be explicit, not globs")
                    payload[name] = self.path(name).read_bytes()
                missing = set(payload) - covered
                if missing:
                    raise WorkflowError(f"untracked release files: {sorted(missing)}")
                payload["EVIDENCE.json"] = json.dumps(
                    {
                        n: json.loads(self.record_path(n).read_text())
                        for n in self.stages
                    },
                    indent=2,
                ).encode()
                payload["SHA256SUMS.txt"] = "".join(
                    f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                    for name, data in sorted(payload.items())
                ).encode()
                output = self.path(bundle["output"])
                if not output.is_relative_to(delivery) or output.suffix != ".zip":
                    raise WorkflowError("release ZIP must be inside delivery directory")
                if output in [p for _, p in pending]:
                    raise WorkflowError("duplicate release output")
                temp = Path(work) / f"{index}.zip"
                with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as z:
                    for name, data in sorted(payload.items()):
                        entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        z.writestr(entry, data)
                with zipfile.ZipFile(temp) as z:
                    if z.testzip() or any(z.read(n) != b for n, b in payload.items()):
                        raise WorkflowError("ZIP readback failed")
                pending.append((temp, output))
                outputs.append(
                    {"file": str(output.relative_to(self.root)), "sha256": sha(temp)}
                )
            # Recheck after reading all payloads, before replacing any existing packages.
            self._tools = None
            for name in self.stages:
                if not self.fresh(name)[0]:
                    raise WorkflowError(
                        f"release inputs changed during packaging: {name}"
                    )
            manifest = {
                "target": target,
                "status": "local engineering review; no order or physical qualification",
                "holds": release.get("holds", []),
                "packages": outputs,
                "evidence": {n: sha(self.record_path(n)) for n in self.stages},
            }
            for temp, output in pending:
                output.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp, output)
            atomic_json(manifest_path, manifest)
        return manifest
