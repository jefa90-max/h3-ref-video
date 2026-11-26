import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const venv = process.env.VIRTUAL_ENV || join(root, ".venv");
const python = process.platform === "win32"
  ? join(venv, "Scripts", "python.exe")
  : join(venv, "bin", "python");
const child = spawnSync(python, ["workbench.py", ...process.argv.slice(2)], {
  cwd: root,
  stdio: "inherit",
});

if (child.error?.code === "ENOENT") {
  const language = process.env.H3_LANG?.trim().toLowerCase().startsWith("en") ? "en" : "zh-CN";
  const messages = new URL(`../workbench/messages/${language}.json`, import.meta.url);
  const { dev_missing_venv: missingVenv } = JSON.parse(readFileSync(messages, "utf8"));
  console.error(missingVenv);
} else if (child.error) {
  console.error(child.error.message);
}
process.exitCode = child.status ?? (child.signal === "SIGINT" ? 130 : 1);
