#!/usr/bin/env node
// Regenerates src/api/schema.d.ts from the backend's live OpenAPI schema. Runs before every
// dev/build/test/typecheck (see package.json's pre* hooks), so the generated types can never
// drift from warden/api/routes_*.py without the very next command failing loudly.
//
// Two steps, not one pipe: openapi-typescript's CLI reads its input as a file path (its
// stdin support does not work through a `uv run` subprocess on Windows, verified by hand),
// so the OpenAPI JSON lands in a gitignored temp file first.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", shell: true, ...options });
  if (result.status !== 0) {
    process.stderr.write(result.stderr ?? "");
    throw new Error(`${command} ${args.join(" ")} exited with ${result.status}`);
  }
  return result.stdout;
}

const tmpDir = mkdtempSync(join(tmpdir(), "warden-openapi-"));
const schemaJsonPath = join(tmpDir, "openapi.json");

try {
  const openapiJson = run("uv", [
    "run",
    "--directory",
    "../backend",
    "python",
    "-m",
    "warden.api.openapi_export",
  ]);
  writeFileSync(schemaJsonPath, openapiJson);

  run("npx", ["openapi-typescript", schemaJsonPath, "-o", "src/api/schema.d.ts"], {
    stdio: "inherit",
  });
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}
