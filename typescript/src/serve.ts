/** Entry point for the conformance runner: `node dist/serve.js`. */
import { Server } from "./server.js";

export function build(): Server {
  const s = new Server();
  s.instructions(
    "MCP from Zero reference server. Built against protocol revision " +
    "2026-07-28, which is stateless - no initialize handshake."
  );
  return s;
}

build().serveStdio();
