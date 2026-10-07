const { spawn } = require("node:child_process");

const electronExecutable = require("electron");
const environment = { ...process.env };
delete environment.ELECTRON_RUN_AS_NODE;

const child = spawn(electronExecutable, ["."], {
  cwd: require("node:path").resolve(__dirname, ".."),
  env: environment,
  stdio: "inherit",
});

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exitCode = code ?? 1;
});
