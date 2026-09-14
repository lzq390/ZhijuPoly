// Scope only the official static stylesheet. Keep keyframes/font declarations
// intact, and put SDK variables on its root instead of the host document.
const scope = ':where([data-editor-engine="react"], [data-ketcher-owner])';
export default function scopeKetcherCss() {
  return {
    postcssPlugin: 'nexpoly-ketcher-css-scope',
    Once(root) {
      if (!/ketcher-react[\\/]dist[\\/]index\.css$/.test(root.source?.input.file || '')) return;
      root.walkRules(rule => {
        for (let parent = rule.parent; parent; parent = parent.parent) {
          if (parent.type === 'atrule' && /keyframes$/i.test(parent.name)) return;
        }
        rule.selectors = rule.selectors.map(selector => {
          if (/^(?::root|html|body)(?=$|[\s.:#[])/.test(selector)) {
            return selector.replace(/^(?::root|html|body)(?:\s+body)?/, scope);
          }
          return `${scope} ${selector}`;
        });
      });
    }
  };
}
scopeKetcherCss.postcss = true;
