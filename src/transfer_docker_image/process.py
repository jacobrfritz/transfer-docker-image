import json
import shutil
import subprocess

from .errors import UserError


class Runner:
    def require(self, *tools):
        for tool in tools:
            if not shutil.which(tool):
                raise UserError(f"Required tool '{tool}' is not on PATH. Install it before running this workflow.")

    def run(self, args, *, cwd=None, capture=True, check=True):
        try:
            result = subprocess.run([str(arg) for arg in args], cwd=cwd, text=True,
                                    encoding="utf-8", errors="replace",
                                    stdout=subprocess.PIPE if capture else None,
                                    stderr=subprocess.PIPE if capture else None)
        except OSError as exc:
            raise UserError(f"Cannot execute {args[0]}: {exc}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "See command output above.").strip()
            raise UserError(f"{args[0]} failed (exit {result.returncode}): {detail}")
        return result

    def json(self, args, *, cwd=None):
        result = self.run(args, cwd=cwd)
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise UserError(f"{args[0]} returned invalid JSON: {result.stdout.strip()}") from exc
