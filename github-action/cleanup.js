const fs = require("fs");

function main() {
  const shouldCleanup = process.env.STATE_cleanup_docker_config_dir === "true";
  const dockerConfigDir = (process.env.STATE_docker_config_dir || "").trim();
  if (!shouldCleanup || !dockerConfigDir) {
    return;
  }

  fs.rmSync(dockerConfigDir, { recursive: true, force: true });
  console.log(`Removed docker-stack Docker config: ${dockerConfigDir}`);
}

try {
  main();
} catch (err) {
  console.warn(`docker-stack cleanup failed: ${err.message || err}`);
}
