// Scope only the official static stylesheet. Keep keyframes/font declarations
// intact, and put SDK variables on its root instead of the host document.
import selectorParser from 'postcss-selector-parser';

const scope = ':where([data-editor-engine="react"], [data-ketcher-owner])';
const scopeNode = selectorParser().astSync(scope).first.first;
const pseudoElement = node => node.type === 'pseudo' && /^(::|:(before|after|first-line|first-letter)$)/.test(node.value);

function scopedSelectors(input) {
  const result = [];
  selectorParser(selectors => selectors.each(selector => {
    const first = selector.first;
    if ((first.type === 'pseudo' && first.value === ':root') ||
        (first.type === 'tag' && ['html', 'body'].includes(first.value))) {
      first.replaceWith(scopeNode.clone());
      const [, combinator, body] = selector.nodes;
      if (combinator?.type === 'combinator' && combinator.value === ' ' && body?.type === 'tag' && body.value === 'body') {
        combinator.remove();
        body.remove();
      }
      result.push(selector.toString());
      return;
    }
    const descendant = selector.clone();
    descendant.prepend(selectorParser.combinator({ value: ' ' }));
    descendant.prepend(scopeNode.clone());
    result.push(descendant.toString());
    // The portal itself can carry the SDK's first class (e.g. Select's menu).
    // Requiring a scoped ancestor alone drops its menu size/scrollbar styles.
    const self = selector.clone();
    const endOfCompound = self.nodes.find(node => node.type === 'combinator' || pseudoElement(node));
    if (endOfCompound) self.insertBefore(endOfCompound, scopeNode.clone());
    else self.append(scopeNode.clone());
    result.push(self.toString());
  })).processSync(input);
  return result.join(', ');
}

export default function scopeKetcherCss() {
  return {
    postcssPlugin: 'nexpoly-ketcher-css-scope',
    Once(root) {
      if (!/ketcher-react[\\/]dist[\\/]index\.css$/.test(root.source?.input.file || '')) return;
      root.walkRules(rule => {
        for (let parent = rule.parent; parent; parent = parent.parent) {
          if (parent.type === 'atrule' && /keyframes$/i.test(parent.name)) return;
        }
        rule.selector = scopedSelectors(rule.selector);
      });
    }
  };
}
scopeKetcherCss.postcss = true;
