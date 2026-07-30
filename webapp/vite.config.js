import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BASE = "/Smaug/";

// `base` has to keep the repo's exact capitalization for GitHub Pages, but a
// browser that remembered a lowercase `/smaug/` will autocomplete straight to
// a 404. Redirect any case variant to the canonical path — dev/preview only,
// so nothing about the built output changes.
function redirectBaseCase(base) {
  const lower = base.toLowerCase();
  const bareLower = lower.replace(/\/$/, "");

  const middleware = (req, res, next) => {
    const [path, query] = req.url.split("?");
    const p = path.toLowerCase();
    let rest = null;
    if (p === bareLower) rest = "";
    else if (p.startsWith(lower)) rest = path.slice(base.length);
    if (rest === null || path.startsWith(base)) return next();
    res.writeHead(302, {
      Location: base + rest + (query ? `?${query}` : ""),
    });
    res.end();
  };

  return {
    name: "redirect-base-case",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}

export default defineConfig({
  plugins: [react(), redirectBaseCase(BASE)],
  base: BASE,
});
