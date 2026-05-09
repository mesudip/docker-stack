const fs = require("fs");
const https = require("https");
const path = require("path");
const { execFileSync } = require("child_process");

function input(name, fallback = "") {
  return process.env[`INPUT_${name.replace(/-/g, "_").toUpperCase()}`] || fallback;
}

function appendLine(file, line) {
  if (!file) return;
  fs.appendFileSync(file, `${line}\n`, { encoding: "utf8" });
}

function setOutput(name, value) {
  appendLine(process.env.GITHUB_OUTPUT, `${name}=${value}`);
}

function exportEnv(name, value) {
  appendLine(process.env.GITHUB_ENV, `${name}=${value}`);
}

function saveState(name, value) {
  appendLine(process.env.GITHUB_STATE, `${name}=${value}`);
}

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    encoding: "utf8",
    stdio: options.stdio || ["ignore", "pipe", "inherit"],
    env: process.env,
  });
}

function requestOidcToken(audience) {
  const requestUrl = process.env.ACTIONS_ID_TOKEN_REQUEST_URL;
  const requestToken = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN;
  if (!requestUrl || !requestToken) {
    throw new Error("GitHub OIDC environment is unavailable. Ensure workflow permissions include id-token: write.");
  }

  const separator = requestUrl.includes("?") ? "&" : "?";
  const url = `${requestUrl}${separator}audience=${encodeURIComponent(audience || "docker-manager")}`;

  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        headers: {
          Authorization: `bearer ${requestToken}`,
          Accept: "application/json",
        },
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () => {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            reject(new Error(`Failed to obtain GitHub OIDC token: HTTP ${res.statusCode}`));
            return;
          }
          try {
            const payload = JSON.parse(body);
            if (!payload.value) {
              reject(new Error("Failed to obtain GitHub OIDC token"));
              return;
            }
            resolve(String(payload.value));
          } catch (err) {
            reject(new Error(`GitHub OIDC response is not valid JSON: ${err.message}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.end();
  });
}

function parseSetupAuthOutput(output) {
  const values = {};
  for (const line of output.split(/\r?\n/)) {
    const index = line.indexOf("=");
    if (index <= 0) continue;
    values[line.slice(0, index)] = line.slice(index + 1);
  }
  return values;
}

async function main() {
  const manager = input("manager").trim();
  if (!manager) {
    throw new Error("Input 'manager' is required.");
  }

  const requestedContext = input("context").trim();
  const contextName = requestedContext || "dm-proxy";
  const providedDockerConfigDir = input("docker-config-dir").trim();
  const dockerConfigDir =
    providedDockerConfigDir ||
    (process.env.RUNNER_TEMP
      ? path.join(process.env.RUNNER_TEMP, "docker-stack", contextName)
      : path.join(process.env.HOME || process.cwd(), ".docker-stack", "actions", contextName));

  let authMode = input("auth-mode", "auto").trim() || "auto";
  const accessToken = input("access-token").trim();
  if (authMode === "auto") {
    authMode = accessToken ? "access-token" : "oidc";
  }
  if (!["oidc", "access-token"].includes(authMode)) {
    throw new Error(`Unsupported auth-mode: ${authMode}`);
  }

  const cliArgs = [manager, "--docker-config-dir", dockerConfigDir];
  if (requestedContext) {
    cliArgs.push("--context", requestedContext);
  }
  if (input("verify-ssl").trim() === "true") {
    cliArgs.push("--verify-ssl");
  }

  if (authMode === "oidc") {
    const oidcToken = await requestOidcToken(input("oidc-audience", "docker-manager"));
    cliArgs.push("--github-oidc-token", oidcToken);
  } else {
    if (!accessToken) {
      throw new Error("access-token auth mode requires access-token input.");
    }
    cliArgs.push("--access-token", accessToken);
  }

  const output = run("docker-stack", ["setup-auth", ...cliArgs]);
  process.stdout.write(output);
  if (!output.endsWith("\n")) {
    process.stdout.write("\n");
  }

  const values = parseSetupAuthOutput(output);
  const resolvedDockerConfig = values.DOCKER_CONFIG || dockerConfigDir;
  const resolvedDockerContext = values.DOCKER_CONTEXT || contextName;
  const managerUrl = values.MANAGER_URL || manager;
  const skipTlsVerify = values.SKIP_TLS_VERIFY || "false";

  exportEnv("DOCKER_CONFIG", resolvedDockerConfig);
  exportEnv("DOCKER_CONTEXT", resolvedDockerContext);
  exportEnv("DOCKER_MANAGER_URL", managerUrl);

  setOutput("docker-config", resolvedDockerConfig);
  setOutput("docker-context", resolvedDockerContext);
  setOutput("manager-url", managerUrl);
  setOutput("skip-tls-verify", skipTlsVerify);

  saveState("docker_config_dir", resolvedDockerConfig);
  saveState("cleanup_docker_config_dir", providedDockerConfigDir ? "false" : "true");
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
