const fs = require("fs");
const postcss = require("postcss");

const sources = [
  "/mnt/d/proj/zhiju/src/shared_modules/polymer/css/polymer-layout.css",
  "/mnt/d/proj/zhiju/src/shared_modules/polymer/css/polymer-capsule.css",
  "/mnt/d/proj/zhiju/src/shared_modules/polymer/css/polymer-cards.css"
];

function splitSelectors(selector) {
  const out = [];
  let current = "";
  let depth = 0;
  let quote = null;
  for (let i = 0; i < selector.length; i += 1) {
    const ch = selector[i];
    if (quote) {
      current += ch;
      if (ch === quote && selector[i - 1] !== "\\") quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    if (ch === "(" || ch === "[") depth += 1;
    if (ch === ")" || ch === "]") depth -= 1;
    if (ch === "," && depth === 0) {
      out.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  if (current.trim()) out.push(current.trim());
  return out;
}

function scopeSelector(selector) {
  if (!selector || selector.startsWith(".polymer-desktop-page")) return selector;
  if (selector.startsWith('html[data-theme="dark"]')) return selector.replace('html[data-theme="dark"]', '.polymer-desktop-page[data-theme="dark"]');
  if (selector.startsWith("html[data-theme='dark']")) return selector.replace("html[data-theme='dark']", ".polymer-desktop-page[data-theme='dark']");
  if (selector === "html" || selector === "body") return ".polymer-desktop-page";
  if (selector.startsWith("html ")) return `.polymer-desktop-page ${selector.slice(5)}`;
  if (selector.startsWith("body ")) return `.polymer-desktop-page ${selector.slice(5)}`;
  return `.polymer-desktop-page ${selector}`;
}

function cleanSource(file) {
  let css = fs.readFileSync(file, "utf8").replace(/^@import\s+url\([^\n]+\);\s*/gm, "");
  if (file.endsWith("polymer-cards.css")) {
    css = css.replace(
      "\n  width: 100%;\n  height: 100%;\n  max-width: 240px;\n  max-height: 240px;\n}\n\n.radar-axis",
      "\n.radar-chart-svg {\n  width: 100%;\n  height: 100%;\n  max-width: 240px;\n  max-height: 240px;\n}\n\n.radar-axis"
    );
  }
  return css;
}

const root = postcss.parse(sources.map(cleanSource).join("\n\n"), { from: undefined });
root.walkRules((rule) => {
  const parent = rule.parent;
  if (parent && parent.type === "atrule" && /keyframes|font-face/i.test(parent.name)) return;
  rule.selector = splitSelectors(rule.selector).map(scopeSelector).join(",\n");
});

const header = `@import url("https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap");

.polymer-desktop-page {
  --color-primary: #4D95FF;
  --color-primary-light: rgba(77, 149, 255, 0.06);
  --color-success: #10b981;
  --color-success-light: rgba(16, 185, 129, 0.08);
  --color-danger: #ef4444;
  --color-danger-light: rgba(239, 68, 68, 0.08);
  --color-warning: #f59e0b;
  --color-warning-light: rgba(245, 158, 11, 0.1);
  --color-bg-app: #f1f5f9;
  --color-bg-card: #ffffff;
  --color-bg-sidebar: #f1f5f9;
  --color-bg-hover: rgba(15, 23, 42, 0.05);
  --color-bg-active: rgba(15, 23, 42, 0.09);
  --color-border: rgba(15, 23, 42, 0.08);
  --color-border-card: rgba(15, 23, 42, 0.06);
  --color-border-divider: transparent;
  --color-text-primary: #0f172a;
  --color-text-secondary: #475569;
  --color-text-tertiary: #94a3b8;
  --color-text-quaternary: #cbd5e1;
  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.02);
  --shadow-md: 0 8px 30px rgba(15, 23, 42, 0.04);
  --shadow-lg: 0 20px 50px rgba(15, 23, 42, 0.06);
  --radius-xs: 4px;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 10px;
  --radius-xl: 14px;
  --radius-full: 9999px;
  --transition-normal: 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  position: relative;
  z-index: 1;
  width: 100%;
  height: calc(100vh - 64px);
  min-height: 720px;
  overflow: hidden;
  background: var(--color-bg-app);
  color: var(--color-text-primary);
  font-family: "Outfit", -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  isolation: isolate;
}

.polymer-desktop-page *,
.polymer-desktop-page *::before,
.polymer-desktop-page *::after { box-sizing: border-box; }
.polymer-desktop-page .btn { border: none; background: transparent; color: inherit; font: inherit; text-decoration: none; user-select: none; }
.polymer-desktop-page .btn--sm { min-height: 30px; }
.polymer-desktop-page .btn:disabled,
.polymer-desktop-page button:disabled { cursor: not-allowed; }
.polymer-desktop-page .app-container { border: none !important; border-radius: 0 !important; background: transparent !important; box-shadow: none !important; width: 100% !important; height: 100% !important; }
.polymer-desktop-page .main-layout { height: 100% !important; width: 100% !important; overflow: hidden !important; }
.polymer-desktop-page .main-content { margin: 0 8px 0 0 !important; height: 100% !important; flex: 1 !important; min-width: 0 !important; }
`;

const footer = `

.polymer-desktop-page--embedded { margin: 0; width: 100%; height: 100%; min-height: 0; background: var(--color-bg-app); }
.polymer-desktop-page .polymer-page-title { position: absolute; top: 0; left: 20px; z-index: 20; margin: 0; font-size: 16px; font-weight: 600; line-height: 1.5; color: var(--color-text-primary); letter-spacing: -0.4px; }
.polymer-desktop-page .polymer-centered-shell { width: 100%; height: 100%; display: flex; justify-content: center; align-items: stretch; overflow: hidden; padding: 0 clamp(24px, 6vw, 160px); background: var(--color-bg-app); }
.polymer-desktop-page .polymer-centered-column { width: min(100%, 1040px); max-width: 1040px; height: 100%; min-width: 0; display: flex; background: var(--color-bg-app); }
.polymer-desktop-page .polymer-centered-column .app-container { width: 100% !important; height: 100% !important; min-width: 0 !important; background: var(--color-bg-app) !important; }
.polymer-desktop-page .polymer-centered-column .main-layout { background: var(--color-bg-app) !important; }
.polymer-desktop-page .polymer-centered-column .main-content { margin: 0 !important; padding: 20px 0 !important; background: var(--color-bg-app) !important; border-radius: 0 !important; box-shadow: none !important; }
.polymer-desktop-page .polymer-module-header { justify-content: flex-end !important; }
.polymer-desktop-page .polymer-centered-column .polymer-workspace { width: 100%; }
.polymer-desktop-page .workspace-analysis-panel { position: absolute !important; top: 0 !important; right: 0 !important; bottom: 0 !important; height: 100% !important; margin: 0 !important; z-index: 90; border: none !important; border-radius: 0 !important; box-shadow: none !important; }
.polymer-desktop-page .analysis-resizer { position: absolute !important; top: 0 !important; bottom: 0 !important; height: 100% !important; margin: 0 !important; z-index: 95; }
.polymer-desktop-page .btn-expand-analysis { right: 0 !important; z-index: 96 !important; }
.polymer-desktop-page .split-button-container.is-disabled { opacity: 0.45; pointer-events: none; }
.polymer-desktop-page .toolbar-actions-group { display: flex; align-items: center; gap: 8px; }
.polymer-desktop-page .spin-icon { animation: polymerSpin 0.8s linear infinite; }
@keyframes polymerSpin { to { transform: rotate(360deg); } }
.polymer-desktop-page .polymer-capsule-feedback { padding: 0 20px 12px; color: var(--color-text-tertiary); font-size: 11px; line-height: 1.4; }
.polymer-desktop-page .polymer-error-graphic { color: var(--color-danger); background-color: var(--color-danger-light); }
.polymer-desktop-page .polymer-result-svg,
.polymer-desktop-page .polymer-result-svg svg,
.polymer-desktop-page .polymer-result-svg img { width: 100%; height: 100%; max-width: 100%; max-height: 100%; object-fit: contain; }
.polymer-desktop-page .polymer-result-smiles-fallback { padding: 12px; color: var(--color-text-secondary); font-family: Consolas, monospace; font-size: 11px; line-height: 1.45; word-break: break-all; text-align: center; }
.polymer-desktop-page #analysis-panel .similarity-grid { grid-template-columns: 1fr; gap: 12px; }
.polymer-desktop-page #analysis-panel .prediction-grid-wrapper { grid-template-columns: 1fr; }
.polymer-desktop-page .btn-close-analysis { width: 28px; height: 28px; border: none; border-radius: var(--radius-sm); background: transparent; color: var(--color-text-secondary); display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all var(--transition-normal); }
.polymer-desktop-page .btn-close-analysis:hover { background-color: var(--color-bg-hover); color: var(--color-text-primary); }
.polymer-desktop-page #btn-toggle-3d { height: 30px !important; min-width: 92px !important; padding: 0 12px !important; display: inline-flex !important; align-items: center !important; justify-content: center !important; gap: 6px !important; white-space: nowrap !important; line-height: 1 !important; border-radius: 6px !important; flex-shrink: 0 !important; }
.polymer-desktop-page #btn-toggle-3d span { display: inline-flex; align-items: center; white-space: nowrap; line-height: 1; }
.polymer-desktop-page #btn-toggle-3d,
.polymer-desktop-page #btn-toggle-3d.active { background: #ffffff !important; border: 1px solid rgba(15, 23, 42, 0.08) !important; color: var(--color-text-secondary) !important; box-shadow: var(--shadow-sm) !important; }
.polymer-desktop-page #btn-toggle-3d:hover,
.polymer-desktop-page #btn-toggle-3d.active:hover { background: var(--color-bg-hover) !important; border-color: rgba(77, 149, 255, 0.22) !important; color: var(--color-primary) !important; transform: translateY(-0.5px); }
.polymer-desktop-page #btn-toggle-3d svg,
.polymer-desktop-page #btn-toggle-3d.active svg { color: currentColor !important; stroke: currentColor !important; }
.polymer-desktop-page .generate-submenu-panel { color: var(--color-text-secondary) !important; }
.polymer-desktop-page .submenu-item,
.polymer-desktop-page .submenu-item span { color: var(--color-text-secondary) !important; font-size: 11.5px; font-weight: 500; line-height: 1.35; }
.polymer-desktop-page .submenu-item:hover,
.polymer-desktop-page .submenu-item:hover span { color: var(--color-text-primary) !important; }
.polymer-desktop-page .submenu-footer,
.polymer-desktop-page .submenu-count { color: var(--color-text-secondary) !important; }
.polymer-desktop-page .submenu-header span { color: var(--color-text-secondary) !important; }
.polymer-desktop-page .real-3d-container { background: #ffffff !important; padding: 0 !important; }
.polymer-desktop-page .polymer-real-3d-preview,
.polymer-desktop-page .polymer-real-3d-content,
.polymer-desktop-page .polymer-real-3d-frame { width: 100% !important; height: 100% !important; min-height: 0 !important; flex: 1 1 auto !important; }
.polymer-desktop-page .polymer-real-3d-frame { border: none !important; border-radius: 0 !important; background: #ffffff !important; }
.polymer-desktop-page .polymer-real-3d-viewer canvas { display: block; }
@media (max-width: 900px) {
  .polymer-desktop-page--embedded { margin: 0; width: 100%; height: 100%; min-height: 0; }
  .polymer-desktop-page .polymer-centered-shell { padding: 0; }
  .polymer-desktop-page .polymer-centered-column { width: 100%; max-width: none; }
  .polymer-desktop-page .main-layout { overflow: auto !important; }
  .polymer-desktop-page .main-content { margin: 0 !important; min-height: 100vh; }
  .polymer-desktop-page .workspace-analysis-panel,
  .polymer-desktop-page .analysis-resizer { display: none !important; }
  .polymer-desktop-page .polymer-module-header { align-items: flex-start; flex-direction: column; gap: 10px; }
  .polymer-desktop-page .polymer-module-header .header-actions { flex-wrap: wrap; }
}
`;
fs.mkdirSync("src/styles", { recursive: true });
fs.writeFileSync("src/styles/polymer-desktop.css", header + root.toString() + footer, "utf8");